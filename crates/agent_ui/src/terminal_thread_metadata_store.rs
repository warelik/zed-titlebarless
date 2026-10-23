use std::path::{Path, PathBuf};

use anyhow::Context as _;
use chrono::{DateTime, Utc};
use collections::{HashMap, HashSet};
use db::{
    sqlez::{
        bindable::Column, domain::Domain, statement::Statement,
        thread_safe_connection::ThreadSafeConnection,
    },
    sqlez_macros::sql,
};
use futures::{FutureExt, future::Shared};
use gpui::{AppContext as _, Entity, Global, Task};
use remote::{RemoteConnectionOptions, same_remote_connection_identity};
use ui::{App, Context, SharedString};
use util::{ResultExt as _, paths::PathStyle};
use workspace::PathList;

use crate::{TerminalId, thread_metadata_store::WorktreePaths};

pub fn init(cx: &mut App) {
    TerminalThreadMetadataStore::init_global(cx);
}

fn same_terminal_remote_connection(
    left: Option<&RemoteConnectionOptions>,
    right: Option<&RemoteConnectionOptions>,
) -> bool {
    if !same_remote_connection_identity(left, right) {
        return false;
    }
    match (left, right) {
        (Some(RemoteConnectionOptions::Ssh(left)), Some(RemoteConnectionOptions::Ssh(right))) => {
            left.args.as_deref().unwrap_or_default() == right.args.as_deref().unwrap_or_default()
        }
        _ => true,
    }
}

struct GlobalTerminalThreadMetadataStore(Entity<TerminalThreadMetadataStore>);
impl Global for GlobalTerminalThreadMetadataStore {}

#[cfg(any(test, feature = "test-support"))]
pub struct TestTerminalMetadataDbName(pub String);
#[cfg(any(test, feature = "test-support"))]
impl Global for TestTerminalMetadataDbName {}

#[cfg(any(test, feature = "test-support"))]
impl TestTerminalMetadataDbName {
    pub fn global(cx: &App) -> String {
        cx.try_global::<Self>()
            .map(|global| global.0.clone())
            .unwrap_or_else(|| {
                let thread = std::thread::current();
                let test_name = thread.name().unwrap_or("unknown_test");
                format!("TERMINAL_THREAD_METADATA_DB_{}", test_name)
            })
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct TerminalThreadMetadata {
    pub terminal_id: TerminalId,
    pub title: SharedString,
    pub custom_title: Option<SharedString>,
    pub created_at: DateTime<Utc>,
    pub worktree_paths: WorktreePaths,
    pub remote_connection: Option<RemoteConnectionOptions>,
    pub working_directory: Option<PathBuf>,
    pub shpool_context: Option<project::terminals::ShpoolSessionContext>,
}

impl TerminalThreadMetadata {
    pub fn folder_paths(&self) -> &PathList {
        self.worktree_paths.folder_path_list()
    }

    pub fn main_worktree_paths(&self) -> &PathList {
        self.worktree_paths.main_worktree_path_list()
    }

    pub fn display_title(&self) -> SharedString {
        compose_terminal_thread_title(
            self.title.as_ref(),
            self.custom_title.as_ref().map(|title| title.as_ref()),
        )
    }

    pub fn editable_title(&self) -> SharedString {
        self.custom_title.clone().unwrap_or_else(|| {
            SharedString::from(terminal_title_without_prefix(self.title.as_ref()).to_string())
        })
    }
}

pub(crate) fn compose_terminal_thread_title(
    terminal_title: &str,
    custom_title: Option<&str>,
) -> SharedString {
    let Some(custom_title) = custom_title.filter(|title| !title.trim().is_empty()) else {
        return SharedString::from(terminal_title.to_string());
    };

    if let Some(prefix) = terminal_title_prefix(terminal_title) {
        SharedString::from(format!("{prefix}{custom_title}"))
    } else {
        SharedString::from(custom_title.to_string())
    }
}

pub(crate) fn terminal_title_without_prefix(title: &str) -> &str {
    terminal_title_prefix(title)
        .map(|prefix| &title[prefix.len()..])
        .unwrap_or(title)
}

pub(crate) fn normalize_terminal_custom_title(
    terminal_title: &str,
    edited_title: SharedString,
) -> Option<SharedString> {
    if edited_title.trim().is_empty()
        || edited_title == terminal_title_without_prefix(terminal_title)
    {
        None
    } else {
        Some(edited_title)
    }
}

pub fn terminal_title_prefix(title: &str) -> Option<&str> {
    let mut prefix_byte_len = 0;
    let mut saw_prefix_character = false;
    let mut saw_whitespace_after_prefix = false;

    let mut chars = title.chars().peekable();
    while let Some(character) = chars.next() {
        if character.is_alphanumeric() {
            return None;
        }

        if character.is_whitespace() {
            if !saw_prefix_character {
                return None;
            }

            prefix_byte_len += character.len_utf8();
            saw_whitespace_after_prefix = true;

            while let Some(character) = chars.peek() {
                if !character.is_whitespace() {
                    break;
                }

                prefix_byte_len += character.len_utf8();
                chars.next();
            }

            break;
        }

        saw_prefix_character = true;
        prefix_byte_len += character.len_utf8();
    }

    if saw_whitespace_after_prefix {
        Some(&title[..prefix_byte_len])
    } else {
        None
    }
}

pub struct TerminalThreadMetadataStore {
    db: TerminalThreadMetadataDb,
    terminals: HashMap<TerminalId, TerminalThreadMetadata>,
    terminals_by_paths: HashMap<PathList, HashSet<TerminalId>>,
    terminals_by_main_paths: HashMap<PathList, HashSet<TerminalId>>,
    reload_task: Option<Shared<Task<()>>>,
    load_error: Option<SharedString>,
    pending_terminal_ops_tx: async_channel::Sender<DbOperation>,
    _db_operations_task: Task<()>,
    deleted_terminal_ids: HashSet<TerminalId>,
    closing_terminal_ids: HashSet<TerminalId>,
    intentionally_closed_terminal_ids: HashSet<TerminalId>,
}

#[derive(Debug, PartialEq)]
enum DbOperation {
    Upsert(TerminalThreadMetadata),
    Delete(TerminalId),
}

impl DbOperation {
    fn id(&self) -> TerminalId {
        match self {
            DbOperation::Upsert(metadata) => metadata.terminal_id,
            DbOperation::Delete(terminal_id) => *terminal_id,
        }
    }
}

impl TerminalThreadMetadataStore {
    #[cfg(not(any(test, feature = "test-support")))]
    pub fn init_global(cx: &mut App) {
        if cx.has_global::<GlobalTerminalThreadMetadataStore>() {
            return;
        }

        let db = TerminalThreadMetadataDb::global(cx);
        let terminal_store = cx.new(|cx| Self::new(db, cx));
        cx.set_global(GlobalTerminalThreadMetadataStore(terminal_store));
    }

    #[cfg(any(test, feature = "test-support"))]
    pub fn init_global(cx: &mut App) {
        let db_name = TestTerminalMetadataDbName::global(cx);
        let db = gpui::block_on(db::open_test_db::<TerminalThreadMetadataDb>(&db_name));
        let terminal_store = cx.new(|cx| Self::new(TerminalThreadMetadataDb(db), cx));
        cx.set_global(GlobalTerminalThreadMetadataStore(terminal_store));
    }

    pub fn try_global(cx: &App) -> Option<Entity<Self>> {
        cx.try_global::<GlobalTerminalThreadMetadataStore>()
            .map(|store| store.0.clone())
    }

    pub fn global(cx: &App) -> Entity<Self> {
        cx.global::<GlobalTerminalThreadMetadataStore>().0.clone()
    }

    pub fn entry(&self, terminal_id: TerminalId) -> Option<&TerminalThreadMetadata> {
        self.terminals.get(&terminal_id)
    }

    pub(crate) fn entry_for_remote_connection(
        &self,
        terminal_id: TerminalId,
        remote_connection: Option<&RemoteConnectionOptions>,
    ) -> Option<&TerminalThreadMetadata> {
        self.entry(terminal_id).filter(|metadata| {
            same_terminal_remote_connection(metadata.remote_connection.as_ref(), remote_connection)
        })
    }

    pub fn entries(&self) -> impl Iterator<Item = &TerminalThreadMetadata> + '_ {
        self.terminals.values()
    }

    pub fn load_error(&self) -> Option<&SharedString> {
        self.load_error.as_ref()
    }

    pub fn reload_task(&self) -> Shared<Task<()>> {
        self.reload_task
            .clone()
            .unwrap_or_else(|| Task::ready(()).shared())
    }

    #[cfg(test)]
    pub(crate) fn has_deleted_terminal_id(&self, terminal_id: TerminalId) -> bool {
        self.deleted_terminal_ids.contains(&terminal_id)
    }

    pub fn entries_for_path<'a>(
        &'a self,
        path_list: &PathList,
        remote_connection: Option<&'a RemoteConnectionOptions>,
    ) -> impl Iterator<Item = &'a TerminalThreadMetadata> + 'a {
        self.terminals_by_paths
            .get(path_list)
            .into_iter()
            .flatten()
            .filter_map(|id| self.terminals.get(id))
            .filter(move |terminal| {
                same_terminal_remote_connection(
                    terminal.remote_connection.as_ref(),
                    remote_connection,
                )
            })
    }

    pub fn entries_for_main_worktree_path<'a>(
        &'a self,
        path_list: &PathList,
        remote_connection: Option<&'a RemoteConnectionOptions>,
    ) -> impl Iterator<Item = &'a TerminalThreadMetadata> + 'a {
        self.terminals_by_main_paths
            .get(path_list)
            .into_iter()
            .flatten()
            .filter_map(|id| self.terminals.get(id))
            .filter(move |terminal| {
                same_terminal_remote_connection(
                    terminal.remote_connection.as_ref(),
                    remote_connection,
                )
            })
    }

    pub fn path_is_referenced_by_terminal(
        &self,
        terminal_id: Option<TerminalId>,
        path: &Path,
        remote_connection: Option<&RemoteConnectionOptions>,
    ) -> bool {
        self.entries().any(|terminal| {
            Some(terminal.terminal_id) != terminal_id
                && same_terminal_remote_connection(
                    terminal.remote_connection.as_ref(),
                    remote_connection,
                )
                && terminal
                    .folder_paths()
                    .paths()
                    .iter()
                    .any(|folder_path| folder_path.as_path() == path)
        })
    }

    pub fn save(&mut self, metadata: TerminalThreadMetadata, cx: &mut Context<Self>) {
        if self.closing_terminal_ids.contains(&metadata.terminal_id)
            || self.intentionally_closed_terminal_ids.contains(&metadata.terminal_id)
        {
            return;
        }
        self.deleted_terminal_ids.remove(&metadata.terminal_id);
        self.save_internal(metadata);
        cx.notify();
    }

    /// A confirmed newly created session must stay recoverable even if its
    /// window disappeared or a close is waiting for creation to finish.
    pub fn retain_created_session(&mut self, metadata: TerminalThreadMetadata, cx: &mut Context<Self>) {
        let id = metadata.terminal_id;
        if self.intentionally_closed_terminal_ids.contains(&id) || self.entry(id).is_some() {
            return;
        }
        self.deleted_terminal_ids.remove(&id);
        self.save_internal(metadata);
        cx.notify();
    }

    pub fn begin_intentional_close(&mut self, terminal_id: TerminalId) -> bool {
        !self.intentionally_closed_terminal_ids.contains(&terminal_id)
            && self.closing_terminal_ids.insert(terminal_id)
    }

    pub fn is_intentionally_closing(&self, terminal_id: TerminalId) -> bool {
        self.closing_terminal_ids.contains(&terminal_id)
    }

    pub fn is_restore_blocked(&self, terminal_id: TerminalId) -> bool {
        self.closing_terminal_ids.contains(&terminal_id)
            || self.intentionally_closed_terminal_ids.contains(&terminal_id)
    }

    pub fn complete_intentional_close(
        &mut self,
        terminal_id: TerminalId,
        succeeded: bool,
        cx: &mut Context<Self>,
    ) {
        self.closing_terminal_ids.remove(&terminal_id);
        if succeeded {
            self.intentionally_closed_terminal_ids.insert(terminal_id);
            self.delete(terminal_id, cx);
        } else {
            cx.notify();
        }
    }

    pub fn rename_terminal(
        &mut self,
        terminal_id: TerminalId,
        title: SharedString,
        cx: &mut Context<Self>,
    ) {
        let Some(mut metadata) = self.entry(terminal_id).cloned() else {
            return;
        };
        let custom_title = normalize_terminal_custom_title(metadata.title.as_ref(), title);
        if metadata.custom_title == custom_title {
            return;
        }

        metadata.custom_title = custom_title;
        self.save_internal(metadata);
        cx.notify();
    }

    pub fn change_worktree_paths(
        &mut self,
        current_folder_paths: &PathList,
        remote_connection: Option<&RemoteConnectionOptions>,
        mutate: impl Fn(&mut WorktreePaths),
        cx: &mut Context<Self>,
    ) {
        let terminal_ids: Vec<_> = self
            .terminals_by_paths
            .get(current_folder_paths)
            .into_iter()
            .flatten()
            .filter(|id| {
                self.terminals.get(id).is_some_and(|terminal| {
                    same_terminal_remote_connection(
                        terminal.remote_connection.as_ref(),
                        remote_connection,
                    )
                })
            })
            .copied()
            .collect();

        if terminal_ids.is_empty() {
            return;
        }

        for terminal_id in terminal_ids {
            if let Some(mut terminal) = self.terminals.get(&terminal_id).cloned() {
                mutate(&mut terminal.worktree_paths);
                self.save_internal(terminal);
            }
        }

        cx.notify();
    }

    pub fn update_worktree_paths_for_project(
        &mut self,
        worktree_paths: &WorktreePaths,
        path_style: PathStyle,
        remote_connection: Option<&RemoteConnectionOptions>,
        cx: &mut Context<Self>,
    ) {
        if worktree_paths.is_empty() {
            return;
        }
        let mut changed = false;
        let terminal_ids: Vec<TerminalId> = self.terminals.keys().copied().collect();
        for terminal_id in terminal_ids {
            if let Some(terminal) = self.terminals.get(&terminal_id).cloned() {
                if terminal.folder_paths().is_empty()
                    && same_terminal_remote_connection(
                        terminal.remote_connection.as_ref(),
                        remote_connection,
                    )
                    && terminal
                        .working_directory
                        .as_ref()
                        .is_some_and(|working_directory| {
                            worktree_paths
                                .folder_path_list()
                                .paths()
                                .iter()
                                .any(|folder_path| {
                                    path_style.starts_with(working_directory, folder_path)
                                })
                        })
                {
                    let mut updated = terminal;
                    updated.worktree_paths = worktree_paths.clone();
                    self.save_internal(updated);
                    changed = true;
                }
            }
        }
        if changed {
            cx.notify();
        }
    }

    /// Shared legacy records must claim the complete locator atomically,
    /// before any project starts its attachment. Fresh IDs are unique until
    /// their creator publishes metadata and therefore have no shared row yet.
    pub fn reserve_shpool_context(
        &mut self,
        terminal_id: TerminalId,
        context: project::terminals::ShpoolSessionContext,
        remote_connection: Option<RemoteConnectionOptions>,
        cx: &mut Context<Self>,
    ) -> anyhow::Result<()> {
        context.validate()?;
        anyhow::ensure!(!self.intentionally_closed_terminal_ids.contains(&terminal_id),
            "terminal was already closed before its daemon reservation");
        if let Some(existing) = self.entry(terminal_id).and_then(|row| row.shpool_context.as_ref()) {
            anyhow::ensure!(existing == &context,
                "another project reserved a different original shpool locator");
            return Ok(());
        }
        // No provisional row is created for a failed NEW spawn.
        self.remember_shpool_context(terminal_id, context, remote_connection, cx);
        Ok(())
    }

    /// Pin only an existing row. Pending closes may learn the locator, but
    /// completed closes must never be resurrected by a late spawn result.
    pub fn remember_shpool_context(
        &mut self,
        terminal_id: TerminalId,
        context: project::terminals::ShpoolSessionContext,
        remote_connection: Option<RemoteConnectionOptions>,
        cx: &mut Context<Self>,
    ) {
        if self.intentionally_closed_terminal_ids.contains(&terminal_id) { return; }
        let Some(mut metadata) = self.entry(terminal_id).cloned() else { return };
        if let Some(existing) = &metadata.shpool_context {
            if !existing.same_daemon(&context) {
                log::warn!("refusing to replace an original shpool context");
            }
            return;
        }
        if metadata.remote_connection.is_none() {
            metadata.remote_connection = remote_connection;
        }
        metadata.shpool_context = Some(context);
        self.save_internal(metadata);
        cx.notify();
    }

    fn save_internal(&mut self, mut metadata: TerminalThreadMetadata) {
        if let Some(existing) = self.terminals.get(&metadata.terminal_id) {
            metadata.created_at = existing.created_at;
            if existing.shpool_context.is_some() {
                metadata.shpool_context = existing.shpool_context.clone();
            }
            if metadata.folder_paths().is_empty() && !existing.folder_paths().is_empty() {
                metadata.worktree_paths = existing.worktree_paths.clone();
            }
            if metadata.remote_connection.is_none() && existing.remote_connection.is_some() {
                metadata.remote_connection = existing.remote_connection.clone();
            }

            if existing.folder_paths() != metadata.folder_paths()
                && let Some(ids) = self.terminals_by_paths.get_mut(existing.folder_paths())
            {
                ids.remove(&metadata.terminal_id);
            }

            if existing.main_worktree_paths() != metadata.main_worktree_paths()
                && let Some(ids) = self
                    .terminals_by_main_paths
                    .get_mut(existing.main_worktree_paths())
            {
                ids.remove(&metadata.terminal_id);
            }
        }

        self.cache_terminal_metadata(metadata.clone());
        self.pending_terminal_ops_tx
            .try_send(DbOperation::Upsert(metadata))
            .log_err();
    }

    fn cache_terminal_metadata(&mut self, metadata: TerminalThreadMetadata) {
        self.terminals
            .insert(metadata.terminal_id, metadata.clone());

        self.terminals_by_paths
            .entry(metadata.folder_paths().clone())
            .or_default()
            .insert(metadata.terminal_id);

        if !metadata.main_worktree_paths().is_empty() {
            self.terminals_by_main_paths
                .entry(metadata.main_worktree_paths().clone())
                .or_default()
                .insert(metadata.terminal_id);
        }
    }

    pub fn delete(&mut self, terminal_id: TerminalId, cx: &mut Context<Self>) {
        if self.reload_task.is_some() {
            self.deleted_terminal_ids.insert(terminal_id);
        }
        if let Some(terminal) = self.terminals.remove(&terminal_id) {
            if let Some(ids) = self.terminals_by_paths.get_mut(terminal.folder_paths()) {
                ids.remove(&terminal_id);
            }
            if !terminal.main_worktree_paths().is_empty()
                && let Some(ids) = self
                    .terminals_by_main_paths
                    .get_mut(terminal.main_worktree_paths())
            {
                ids.remove(&terminal_id);
            }
        }
        self.pending_terminal_ops_tx
            .try_send(DbOperation::Delete(terminal_id))
            .log_err();
        cx.notify();
    }

    fn new(db: TerminalThreadMetadataDb, cx: &mut Context<Self>) -> Self {
        let (tx, rx) = async_channel::unbounded();
        let _db_operations_task = cx.background_spawn({
            let db = db.clone();
            async move {
                while let Ok(first_update) = rx.recv().await {
                    let mut updates = vec![first_update];
                    while let Ok(update) = rx.try_recv() {
                        updates.push(update);
                    }
                    let updates = Self::dedup_db_operations(updates);
                    for operation in updates {
                        match operation {
                            DbOperation::Upsert(metadata) => {
                                db.save(metadata).await.log_err();
                            }
                            DbOperation::Delete(terminal_id) => {
                                db.delete(terminal_id).await.log_err();
                            }
                        }
                    }
                }
            }
        });

        let mut this = Self {
            db,
            terminals: HashMap::default(),
            terminals_by_paths: HashMap::default(),
            terminals_by_main_paths: HashMap::default(),
            deleted_terminal_ids: HashSet::default(),
            closing_terminal_ids: HashSet::default(),
            intentionally_closed_terminal_ids: HashSet::default(),
            reload_task: None,
            load_error: None,
            pending_terminal_ops_tx: tx,
            _db_operations_task,
        };
        this.reload(cx);
        this
    }

    fn dedup_db_operations(operations: Vec<DbOperation>) -> Vec<DbOperation> {
        let mut ops = HashMap::default();
        for operation in operations.into_iter().rev() {
            if ops.contains_key(&operation.id()) {
                continue;
            }
            ops.insert(operation.id(), operation);
        }
        ops.into_values().collect()
    }

    fn reload(&mut self, cx: &mut Context<Self>) {
        let db = self.db.clone();
        self.reload_task = Some(
            cx.spawn(async move |this, cx| {
                let rows = cx
                    .background_spawn(async move {
                        db.list()
                            .context("Failed to fetch terminal thread metadata")
                    })
                    .await;

                this.update(cx, |this, cx| {
                    let rows = match rows {
                        Ok(loaded) => {
                            this.load_error = (loaded.failed_rows > 0).then(|| {
                                log::warn!("Unable to decode {} saved terminal metadata rows; originals retained", loaded.failed_rows);
                                format!("Some saved terminals could not be restored ({}). Their saved data was kept.", loaded.failed_rows).into()
                            });
                            loaded.rows
                        }
                        Err(error) => {
                            log::error!("Failed to fetch terminal thread metadata: {error:#}");
                            this.load_error = Some("Saved terminals could not be loaded. Their saved data was kept.".into());
                            Vec::new()
                        }
                    };
                    for row in rows {
                        if !this.deleted_terminal_ids.contains(&row.terminal_id)
                            && !this.terminals.contains_key(&row.terminal_id)
                        {
                            this.cache_terminal_metadata(row);
                        }
                    }

                    this.deleted_terminal_ids.clear();
                    this.reload_task = None;
                    cx.notify();
                })
                .ok();
            })
            .shared(),
        );
    }
}

struct TerminalThreadMetadataDb(ThreadSafeConnection);

struct LoadedTerminalMetadata {
    rows: Vec<TerminalThreadMetadata>,
    failed_rows: usize,
}

impl Domain for TerminalThreadMetadataDb {
    const NAME: &str = stringify!(TerminalThreadMetadataDb);

    const MIGRATIONS: &[&str] = &[sql!(
        CREATE TABLE IF NOT EXISTS sidebar_terminal_threads(
            terminal_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            custom_title TEXT,
            created_at TEXT NOT NULL,
            working_directory TEXT,
            folder_paths TEXT,
            folder_paths_order TEXT,
            main_worktree_paths TEXT,
            main_worktree_paths_order TEXT,
            remote_connection TEXT
        ) STRICT;
    ), r#"
        UPDATE sidebar_terminal_threads
        SET remote_connection = CASE WHEN json_valid(remote_connection)
            THEN json_remove(remote_connection, '$.Ssh.password')
            ELSE remote_connection END
        WHERE remote_connection IS NOT NULL;
    "#, sql!(
        ALTER TABLE sidebar_terminal_threads ADD COLUMN shpool_context TEXT;
    )];
}

db::static_connection!(TerminalThreadMetadataDb, []);

fn remote_connection_for_persistence(
    remote_connection: Option<RemoteConnectionOptions>,
) -> Option<RemoteConnectionOptions> {
    remote_connection.map(|connection| match connection {
        RemoteConnectionOptions::Ssh(mut options) => {
            options.password = None;
            RemoteConnectionOptions::Ssh(options)
        }
        connection => connection,
    })
}

impl TerminalThreadMetadataDb {
    fn list(&self) -> anyhow::Result<LoadedTerminalMetadata> {
        let mut statement = Statement::prepare(
            &self.0,
            "SELECT terminal_id, title, custom_title, created_at, \
            working_directory, folder_paths, folder_paths_order, main_worktree_paths, \
            main_worktree_paths_order, remote_connection, shpool_context \
            FROM sidebar_terminal_threads \
            ORDER BY created_at DESC",
        )?;
        // Decode each row independently. Never treat an invalid remote host as
        // local, rewrite its payload, or let it hide unrelated valid terminals.
        let decoded = statement.map(|row| Ok(row.column::<TerminalThreadMetadata>()))?;
        let mut loaded = LoadedTerminalMetadata { rows: Vec::new(), failed_rows: 0 };
        for row in decoded {
            match row {
                Ok(row) => loaded.rows.push(row),
                Err(_) => loaded.failed_rows += 1,
            }
        }
        Ok(loaded)
    }

    pub async fn save(&self, row: TerminalThreadMetadata) -> anyhow::Result<()> {
        let terminal_id = row.terminal_id.to_key_string();
        let title = row.title.to_string();
        let custom_title = row.custom_title.as_ref().map(ToString::to_string);
        let created_at = row.created_at.to_rfc3339();
        let working_directory = row
            .working_directory
            .as_ref()
            .map(|path| path.to_string_lossy().into_owned());
        let serialized = row.folder_paths().serialize();
        let (folder_paths, folder_paths_order) = if row.folder_paths().is_empty() {
            (None, None)
        } else {
            (Some(serialized.paths), Some(serialized.order))
        };
        let main_serialized = row.main_worktree_paths().serialize();
        let (main_worktree_paths, main_worktree_paths_order) =
            if row.main_worktree_paths().is_empty() {
                (None, None)
            } else {
                (Some(main_serialized.paths), Some(main_serialized.order))
            };
        let remote_connection = remote_connection_for_persistence(row.remote_connection)
            .as_ref()
            .map(serde_json::to_string)
            .transpose()
            .context("serialize terminal thread remote connection")?;

        let shpool_context = row.shpool_context.as_ref().map(|context| {
            context.validate()?;
            serde_json::to_string(context).context("serialize original shpool context")
        }).transpose()?;

        self.write(move |conn| {
            let sql = "INSERT INTO sidebar_terminal_threads(terminal_id, title, custom_title, created_at, working_directory, folder_paths, folder_paths_order, main_worktree_paths, main_worktree_paths_order, remote_connection, shpool_context) \
                       VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11) \
                       ON CONFLICT(terminal_id) DO UPDATE SET \
                           title = excluded.title, \
                           custom_title = excluded.custom_title, \
                           created_at = sidebar_terminal_threads.created_at, \
                           working_directory = excluded.working_directory, \
                           folder_paths = COALESCE(excluded.folder_paths, sidebar_terminal_threads.folder_paths), \
                           folder_paths_order = COALESCE(excluded.folder_paths_order, sidebar_terminal_threads.folder_paths_order), \
                           main_worktree_paths = COALESCE(excluded.main_worktree_paths, sidebar_terminal_threads.main_worktree_paths), \
                           main_worktree_paths_order = COALESCE(excluded.main_worktree_paths_order, sidebar_terminal_threads.main_worktree_paths_order), \
                           remote_connection = COALESCE(excluded.remote_connection, sidebar_terminal_threads.remote_connection), \
                           shpool_context = COALESCE(sidebar_terminal_threads.shpool_context, excluded.shpool_context)";
            let mut stmt = Statement::prepare(conn, sql)?;
            let mut i = stmt.bind(&terminal_id, 1)?;
            i = stmt.bind(&title, i)?;
            i = stmt.bind(&custom_title, i)?;
            i = stmt.bind(&created_at, i)?;
            i = stmt.bind(&working_directory, i)?;
            i = stmt.bind(&folder_paths, i)?;
            i = stmt.bind(&folder_paths_order, i)?;
            i = stmt.bind(&main_worktree_paths, i)?;
            i = stmt.bind(&main_worktree_paths_order, i)?;
            i = stmt.bind(&remote_connection, i)?;
            stmt.bind(&shpool_context, i)?;
            stmt.exec()
        })
        .await
    }

    pub async fn delete(&self, terminal_id: TerminalId) -> anyhow::Result<()> {
        let terminal_id = terminal_id.to_key_string();
        self.write(move |conn| {
            let mut stmt = Statement::prepare(
                conn,
                "DELETE FROM sidebar_terminal_threads WHERE terminal_id = ?",
            )?;
            stmt.bind(&terminal_id, 1)?;
            stmt.exec()
        })
        .await
    }
}

impl Column for TerminalThreadMetadata {
    fn column(statement: &mut Statement, start_index: i32) -> anyhow::Result<(Self, i32)> {
        let (terminal_id, next): (String, i32) = Column::column(statement, start_index)?;
        let (title, next): (String, i32) = Column::column(statement, next)?;
        let (custom_title, next): (Option<String>, i32) = Column::column(statement, next)?;
        let (created_at, next): (String, i32) = Column::column(statement, next)?;
        let (working_directory, next): (Option<String>, i32) = Column::column(statement, next)?;
        let (folder_paths_str, next): (Option<String>, i32) = Column::column(statement, next)?;
        let (folder_paths_order_str, next): (Option<String>, i32) =
            Column::column(statement, next)?;
        let (main_worktree_paths_str, next): (Option<String>, i32) =
            Column::column(statement, next)?;
        let (main_worktree_paths_order_str, next): (Option<String>, i32) =
            Column::column(statement, next)?;
        let (remote_connection_json, next): (Option<String>, i32) =
            Column::column(statement, next)?;
        let (shpool_context_json, next): (Option<String>, i32) =
            Column::column(statement, next)?;
        let shpool_context = shpool_context_json.as_deref()
            .map(serde_json::from_str::<project::terminals::ShpoolSessionContext>)
            .transpose().context("deserialize original shpool context")?;
        if let Some(context) = &shpool_context { context.validate()?; }

        let folder_paths = folder_paths_str
            .map(|paths| {
                PathList::deserialize(&util::path_list::SerializedPathList {
                    paths,
                    order: folder_paths_order_str.unwrap_or_default(),
                })
            })
            .unwrap_or_default();

        let main_worktree_paths = main_worktree_paths_str
            .map(|paths| {
                PathList::deserialize(&util::path_list::SerializedPathList {
                    paths,
                    order: main_worktree_paths_order_str.unwrap_or_default(),
                })
            })
            .unwrap_or_default();

        let remote_connection = remote_connection_json
            .as_deref()
            .map(serde_json::from_str::<RemoteConnectionOptions>)
            .transpose()
            .context("deserialize terminal thread remote connection")?;
        // Defense for legacy rows read outside the normal migration/open path.
        let remote_connection = remote_connection_for_persistence(remote_connection);

        let worktree_paths = WorktreePaths::from_path_lists(main_worktree_paths, folder_paths)
            .context("deserialize terminal thread worktree paths")?;

        Ok((
            TerminalThreadMetadata {
                shpool_context,
                terminal_id: TerminalId::from_key_string(&terminal_id)?,
                title: SharedString::from(title),
                custom_title: custom_title
                    .filter(|title| !title.trim().is_empty())
                    .map(SharedString::from),
                created_at: DateTime::parse_from_rfc3339(&created_at)?.with_timezone(&Utc),
                worktree_paths,
                remote_connection,
                working_directory: working_directory.map(PathBuf::from),
            },
            next,
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use gpui::TestAppContext;
    use std::path::Path;

    fn init_test(cx: &mut TestAppContext) {
        cx.update(|cx| {
            TerminalThreadMetadataStore::init_global(cx);
        });
        cx.run_until_parked();
    }

    fn metadata(title: &str, worktree_paths: WorktreePaths) -> TerminalThreadMetadata {
        let now = Utc::now();
        TerminalThreadMetadata {
            shpool_context: None,
            terminal_id: TerminalId::new(),
            title: SharedString::from(title.to_string()),
            custom_title: None,
            created_at: now,
            worktree_paths,
            remote_connection: None,
            working_directory: None,
        }
    }

    #[gpui::test]
    async fn test_intentional_close_blocks_restore_and_tombstones_stale_snapshots(
        cx: &mut TestAppContext,
    ) {
        init_test(cx);
        let metadata = metadata("Closing", WorktreePaths::default());
        let terminal_id = metadata.terminal_id;
        cx.update(|cx| {
            TerminalThreadMetadataStore::global(cx).update(cx, |store, cx| {
                store.save(metadata.clone(), cx);
                assert!(store.begin_intentional_close(terminal_id));
                assert!(store.is_intentionally_closing(terminal_id));
                store.save(metadata.clone(), cx);
                store.complete_intentional_close(terminal_id, false, cx);
                assert!(!store.is_intentionally_closing(terminal_id));
                assert!(store.begin_intentional_close(terminal_id));
                store.complete_intentional_close(terminal_id, true, cx);
                assert!(store.is_restore_blocked(terminal_id));
                assert!(!store.begin_intentional_close(terminal_id));
                store.save(metadata, cx);
                assert!(
                    store.entry(terminal_id).is_none(),
                    "a stale restore snapshot must not resurrect an intentionally closed terminal"
                );
            });
        });
    }

    #[gpui::test]
    async fn test_terminal_daemon_locator_remains_immutable_in_cache_and_database(
        cx: &mut TestAppContext,
    ) {
        init_test(cx);
        let store = cx.update(|cx| TerminalThreadMetadataStore::global(cx));
        let paths = PathList::new(&[PathBuf::from("/original")]);
        let mut original = metadata("Original", WorktreePaths::from_folder_paths(&paths));
        let context = project::terminals::ShpoolSessionContext::for_test();
        original.shpool_context = Some(context.clone());
        let mut changed = original.clone();
        changed.title = "Retitled".into();
        changed.remote_connection = Some(RemoteConnectionOptions::Mock(remote::MockConnectionOptions { id: 77 }));
        changed.shpool_context = Some(project::terminals::ShpoolSessionContext::new(
            "/different/shpool".into(), "/tmp/different.socket".into(),
        ).unwrap());
        let changed_paths = PathList::new(&[PathBuf::from("/reordered")]);
        changed.worktree_paths = WorktreePaths::from_folder_paths(&changed_paths);
        store.update(cx, |store, cx| {
            store.save(original.clone(), cx);
            store.save(changed.clone(), cx);
        });
        store.read_with(cx, |store, _| {
            let saved = store.entry(original.terminal_id).unwrap();
            assert_eq!(saved.shpool_context.as_ref(), Some(&context));
            assert_eq!(saved.remote_connection,
                Some(RemoteConnectionOptions::Mock(remote::MockConnectionOptions { id: 77 })),
                "connection routing keeps its existing update contract");
            assert_eq!(saved.folder_paths(), &changed_paths);
            assert_eq!(saved.title.as_ref(), "Retitled");
        });
        let db = TerminalThreadMetadataDb(
            db::open_test_db::<TerminalThreadMetadataDb>("TERMINAL_DAEMON_LOCATOR_ROUNDTRIP").await,
        );
        db.save(original.clone()).await.unwrap();
        db.save(changed).await.unwrap();
        let loaded = db.list().unwrap();
        assert_eq!(loaded.failed_rows, 0);
        assert_eq!(loaded.rows.len(), 1);
        assert_eq!(loaded.rows[0].shpool_context.as_ref(), Some(&context));
        assert_eq!(loaded.rows[0].remote_connection,
            Some(RemoteConnectionOptions::Mock(remote::MockConnectionOptions { id: 77 })));
        assert_eq!(loaded.rows[0].folder_paths(), &changed_paths);
        let legacy = metadata("Legacy", WorktreePaths::from_folder_paths(&paths));
        db.save(legacy.clone()).await.unwrap();
        assert!(db.list().unwrap().rows.iter()
            .find(|row| row.terminal_id == legacy.terminal_id).unwrap().shpool_context.is_none());
    }

    #[gpui::test]
    async fn test_corrupt_daemon_locator_is_isolated_without_rewriting_legacy_rows(
        _cx: &mut TestAppContext,
    ) {
        let db = TerminalThreadMetadataDb(
            db::open_test_db::<TerminalThreadMetadataDb>("TERMINAL_DAEMON_LOCATOR_CORRUPTION").await,
        );
        let paths = PathList::new(&[PathBuf::from("/saved")]);
        let valid = metadata("Valid legacy", WorktreePaths::from_folder_paths(&paths));
        let mut invalid = metadata("Invalid locator", WorktreePaths::from_folder_paths(&paths));
        let context = project::terminals::ShpoolSessionContext::for_test();
        invalid.shpool_context = Some(context.clone());
        db.save(valid.clone()).await.unwrap();
        db.save(invalid.clone()).await.unwrap();
        for raw in [
            "not-json",
            r#"{"program":"relative","socket":"/tmp/original.socket"}"#,
            r#"{"program":"/shpool","socket":"relative"}"#,
            r#"{"program":"/shpool","socket":"/tmp/original.socket","env":{"TOKEN":"synthetic"}}"#,
        ] {
            let key = invalid.terminal_id.to_key_string();
            let raw_value = raw.to_string();
            db.write(move |conn| {
                let mut statement = Statement::prepare(conn,
                    "UPDATE sidebar_terminal_threads SET shpool_context = ?1 WHERE terminal_id = ?2")?;
                let next = statement.bind(&raw_value, 1)?;
                statement.bind(&key, next)?;
                statement.exec()
            }).await.unwrap();
            let loaded = db.list().unwrap();
            assert_eq!(loaded.failed_rows, 1);
            assert_eq!(loaded.rows, vec![valid.clone()]);
            let raw_values = db.select::<String>(
                "SELECT shpool_context FROM sidebar_terminal_threads WHERE shpool_context IS NOT NULL",
            ).unwrap()().unwrap();
            assert_eq!(raw_values, vec![raw.to_string()]);
        }
        let key = invalid.terminal_id.to_key_string();
        let repaired = serde_json::to_string(&context).unwrap();
        db.write(move |conn| {
            let mut statement = Statement::prepare(conn,
                "UPDATE sidebar_terminal_threads SET shpool_context = ?1 WHERE terminal_id = ?2")?;
            let next = statement.bind(&repaired, 1)?;
            statement.bind(&key, next)?;
            statement.exec()
        }).await.unwrap();
        let loaded = db.list().unwrap();
        assert_eq!(loaded.failed_rows, 0);
        assert!(loaded.rows.contains(&valid));
        assert!(loaded.rows.contains(&invalid));
    }

    #[test]
    fn test_terminal_metadata_persistence_strips_ssh_password() {
        let connection = RemoteConnectionOptions::Ssh(remote::SshConnectionOptions {
            host: "example.com".into(),
            username: Some("alice".into()),
            port: Some(2222),
            password: Some("plaintext-secret".into()),
            ..Default::default()
        });

        let persisted = remote_connection_for_persistence(Some(connection))
            .expect("remote connection should be retained");
        let RemoteConnectionOptions::Ssh(options) = persisted else {
            panic!("SSH connection should remain SSH")
        };
        assert_eq!(options.host.to_string(), "example.com");
        assert_eq!(options.username.as_deref(), Some("alice"));
        assert_eq!(options.port, Some(2222));
        assert_eq!(options.password, None);
        assert!(
            !serde_json::to_string(&options)
                .expect("sanitized options should serialize")
                .contains("plaintext-secret")
        );
    }

    #[gpui::test]
    async fn test_legacy_terminal_password_migration_scrubs_rows_and_loaded_values(
        _cx: &mut TestAppContext,
    ) {
        let db = TerminalThreadMetadataDb(
            db::open_test_db::<TerminalThreadMetadataDb>("TERMINAL_LEGACY_PASSWORD_MIGRATION").await,
        );
        let paths = PathList::new(&[PathBuf::from("/z-project"), PathBuf::from("/a-project")]);
        let mut legacy = metadata("Legacy terminal", WorktreePaths::from_folder_paths(&paths));
        legacy.custom_title = Some("Custom title".into());
        legacy.working_directory = Some(PathBuf::from("/z-project/subdir"));
        legacy.remote_connection = Some(RemoteConnectionOptions::Ssh(remote::SshConnectionOptions {
            host: "example.com".into(), username: Some("alice".into()), port: Some(2222),
            password: Some("synthetic-legacy-secret".into()), ..Default::default()
        }));
        db.save(legacy.clone()).await.unwrap();
        let mut raw = serde_json::to_value(legacy.remote_connection.as_ref().unwrap()).unwrap();
        raw["Ssh"]["password"] = serde_json::Value::String("synthetic-legacy-secret".into());
        let raw = serde_json::to_string(&raw).unwrap();
        db.write(move |conn| {
            let mut statement = Statement::prepare(conn, "UPDATE sidebar_terminal_threads SET remote_connection = ?1")?;
            statement.bind(&raw, 1)?;
            statement.exec()
        }).await.unwrap();
        let mut expected = legacy;
        expected.remote_connection = remote_connection_for_persistence(expected.remote_connection);
        // Loading must never expose the historical password, even before scrub.
        let loaded = db.list().unwrap();
        assert_eq!(loaded.failed_rows, 0);
        assert_eq!(loaded.rows, vec![expected.clone()]);
        let before = db.select::<String>("SELECT remote_connection FROM sidebar_terminal_threads").unwrap()().unwrap();
        assert!(before[0].contains("synthetic-legacy-secret"));
        for _ in 0..2 {
            db.write(|conn| {
                Statement::prepare(conn, <TerminalThreadMetadataDb as Domain>::MIGRATIONS[1])?.exec()
            }).await.unwrap();
        }
        let after = db.select::<String>("SELECT remote_connection FROM sidebar_terminal_threads").unwrap()().unwrap();
        assert!(!after[0].contains("synthetic-legacy-secret"));
        let decoded: serde_json::Value = serde_json::from_str(&after[0]).unwrap();
        assert!(decoded["Ssh"].get("password").is_none());
        let loaded = db.list().unwrap();
        assert_eq!(loaded.failed_rows, 0);
        assert_eq!(loaded.rows, vec![expected]);
        // Malformed legacy payloads and unrelated connection variants must not
        // abort the scrub or silently become a local connection.
        for raw in [Some("not-json".to_string()), Some(r#"{"Mock":{"id":7}}"#.to_string()), None] {
            let value = raw.clone();
            db.write(move |conn| {
                let mut statement = Statement::prepare(conn, "UPDATE sidebar_terminal_threads SET remote_connection = ?1")?;
                statement.bind(&value, 1)?;
                statement.exec()
            }).await.unwrap();
            db.write(|conn| {
                Statement::prepare(conn, <TerminalThreadMetadataDb as Domain>::MIGRATIONS[1])?.exec()
            }).await.unwrap();
            let values = db.select::<Option<String>>("SELECT remote_connection FROM sidebar_terminal_threads").unwrap()().unwrap();
            assert_eq!(values, vec![raw]);
        }
    }

    #[gpui::test]
    async fn test_corrupt_terminal_metadata_rows_are_isolated_and_recoverable(cx: &mut TestAppContext) {
        init_test(cx);
        let store = cx.update(|cx| TerminalThreadMetadataStore::global(cx));
        let db = store.read_with(cx, |store, _| store.db.clone());
        let paths = PathList::new(&[Path::new("/saved-project")]);
        let valid = metadata("Valid", WorktreePaths::from_folder_paths(&paths));
        let malformed = metadata("Malformed remote", WorktreePaths::from_folder_paths(&paths));
        let unknown = metadata("Unknown remote", WorktreePaths::from_folder_paths(&paths));
        let bad_date = metadata("Invalid date", WorktreePaths::from_folder_paths(&paths));
        let all = [valid.clone(), malformed.clone(), unknown.clone(), bad_date.clone()];
        for row in &all {
            db.save(row.clone()).await.unwrap();
        }
        for (id, raw) in [
            (malformed.terminal_id, "not-json"),
            (unknown.terminal_id, r#"{"UnknownFutureTransport":{"host":"example.com"}}"#),
        ] {
            let key = id.to_key_string();
            let raw = raw.to_string();
            db.write(move |conn| {
                let mut statement = Statement::prepare(conn, "UPDATE sidebar_terminal_threads SET remote_connection = ?1 WHERE terminal_id = ?2")?;
                let next = statement.bind(&raw, 1)?;
                statement.bind(&key, next)?;
                statement.exec()
            }).await.unwrap();
        }
        let key = bad_date.terminal_id.to_key_string();
        db.write(move |conn| {
            let mut statement = Statement::prepare(conn, "UPDATE sidebar_terminal_threads SET created_at = 'invalid-date' WHERE terminal_id = ?1")?;
            statement.bind(&key, 1)?;
            statement.exec()
        }).await.unwrap();
        store.update(cx, |store, cx| store.reload(cx));
        store.read_with(cx, |store, _| store.reload_task()).await;
        store.read_with(cx, |store, _| {
            assert_eq!(store.entry(valid.terminal_id), Some(&valid));
            assert_eq!(store.entries().count(), 1);
            assert!(store.entries_for_path(&paths, None).any(|row| row.terminal_id == valid.terminal_id));
            for row in [&malformed, &unknown, &bad_date] {
                assert!(store.entry(row.terminal_id).is_none(), "corrupt metadata must not become a local terminal");
            }
            assert!(store.load_error().unwrap().contains("(3)"));
        });
        // Loading is read-only: preserve the exact original payloads for recovery.
        let raw = db.select::<String>("SELECT remote_connection FROM sidebar_terminal_threads WHERE remote_connection IS NOT NULL ORDER BY title").unwrap()().unwrap();
        assert_eq!(raw, vec!["not-json", r#"{"UnknownFutureTransport":{"host":"example.com"}}"#]);
        let date = db.select::<String>("SELECT created_at FROM sidebar_terminal_threads WHERE title = 'Invalid date'").unwrap()().unwrap();
        assert_eq!(date, vec!["invalid-date"]);
        // Once the stored values are repaired, reload restores every row and
        // clears the visible error without restarting or deleting anything.
        let restored_date = bad_date.created_at.to_rfc3339();
        db.write(move |conn| {
            Statement::prepare(conn, "UPDATE sidebar_terminal_threads SET remote_connection = NULL")?.exec()?;
            let mut statement = Statement::prepare(conn, "UPDATE sidebar_terminal_threads SET created_at = ?1 WHERE title = 'Invalid date'")?;
            statement.bind(&restored_date, 1)?;
            statement.exec()
        }).await.unwrap();
        store.update(cx, |store, cx| store.reload(cx));
        store.read_with(cx, |store, _| store.reload_task()).await;
        store.read_with(cx, |store, _| {
            assert!(store.load_error().is_none());
            assert_eq!(store.entries().count(), all.len());
            for row in &all {
                assert_eq!(store.entry(row.terminal_id), Some(row));
            }
        });
    }

    #[gpui::test]
    async fn test_invalid_terminal_worktree_pairs_do_not_adopt_other_project_context(
        cx: &mut TestAppContext,
    ) {
        init_test(cx);
        let store = cx.update(|cx| TerminalThreadMetadataStore::global(cx));
        let db = store.read_with(cx, |store, _| store.db.clone());
        let saved_paths = PathList::new(&[Path::new("/saved-project")]);
        let multiple_paths = PathList::new(&[
            Path::new("/saved-project"),
            Path::new("/another-saved-project"),
        ]);
        let empty_paths = PathList::default();
        let valid = metadata("Valid saved roots", WorktreePaths::from_folder_paths(&saved_paths));
        let rootless = metadata("Valid rootless terminal", WorktreePaths::default());
        let linked_main_paths = PathList::new(&[Path::new("/main-project"), Path::new("/main-project")]);
        let linked_folder_paths = PathList::new(&[Path::new("/z-linked"), Path::new("/a-linked")]);
        let linked = metadata("Valid linked roots",
            WorktreePaths::from_path_lists(linked_main_paths, linked_folder_paths).unwrap());
        let mut invalid = metadata("Invalid path pairs", WorktreePaths::from_folder_paths(&saved_paths));
        invalid.working_directory = Some(PathBuf::from("/adopting-project/subdir"));
        for row in [&valid, &rootless, &linked, &invalid] {
            db.save(row.clone()).await.unwrap();
        }
        let terminal_key = invalid.terminal_id.to_key_string();
        let adopting_paths = PathList::new(&[Path::new("/adopting-project")]);
        let adopting_context = WorktreePaths::from_folder_paths(&adopting_paths);
        for (main_paths, folder_paths) in [
            (&multiple_paths, &saved_paths),
            (&saved_paths, &multiple_paths),
            (&empty_paths, &saved_paths),
            (&saved_paths, &empty_paths),
        ] {
            let main = main_paths.serialize();
            let folders = folder_paths.serialize();
            let raw_paths = (main.paths, main.order, folders.paths, folders.order);
            let update_paths = raw_paths.clone();
            let update_key = terminal_key.clone();
            db.write(move |connection| {
                let mut statement = Statement::prepare(connection,
                    "UPDATE sidebar_terminal_threads SET main_worktree_paths = ?1, main_worktree_paths_order = ?2, folder_paths = ?3, folder_paths_order = ?4 WHERE terminal_id = ?5")?;
                let next = statement.bind(&update_paths, 1)?;
                statement.bind(&update_key, next)?;
                statement.exec()
            }).await.unwrap();
            store.update(cx, |store, cx| store.reload(cx));
            store.read_with(cx, |store, _| store.reload_task()).await;
            // A corrupted pair must not become a rootless cached terminal that
            // can be adopted by this unrelated project's matching cwd.
            store.update(cx, |store, cx| {
                store.update_worktree_paths_for_project(
                    &adopting_context,
                    PathStyle::Unix,
                    None,
                    cx,
                );
            });
            cx.run_until_parked();
            let stored = db.select_bound::<String, (String, String, String, String)>(
                "SELECT main_worktree_paths, main_worktree_paths_order, folder_paths, folder_paths_order FROM sidebar_terminal_threads WHERE terminal_id = ?1",
            ).unwrap()(terminal_key.clone()).unwrap();
            assert_eq!(stored, vec![raw_paths], "failed restoration must preserve every raw path field");
            store.read_with(cx, |store, _| {
                assert_eq!(store.entry(valid.terminal_id), Some(&valid));
                assert_eq!(store.entry(rootless.terminal_id), Some(&rootless));
                assert_eq!(store.entry(linked.terminal_id), Some(&linked));
                assert!(store.entry(invalid.terminal_id).is_none());
                assert_eq!(store.entries().count(), 3);
                assert!(store.load_error().unwrap().contains("(1)"));
            });
        }
        let main = saved_paths.serialize();
        let folders = saved_paths.serialize();
        let restored_paths = (main.paths, main.order, folders.paths, folders.order);
        db.write(move |connection| {
            let mut statement = Statement::prepare(connection,
                "UPDATE sidebar_terminal_threads SET main_worktree_paths = ?1, main_worktree_paths_order = ?2, folder_paths = ?3, folder_paths_order = ?4 WHERE terminal_id = ?5")?;
            let next = statement.bind(&restored_paths, 1)?;
            statement.bind(&terminal_key, next)?;
            statement.exec()
        }).await.unwrap();
        store.update(cx, |store, cx| store.reload(cx));
        store.read_with(cx, |store, _| store.reload_task()).await;
        store.read_with(cx, |store, _| {
            assert!(store.load_error().is_none());
            assert_eq!(store.entries().count(), 4);
            for row in [&valid, &rootless, &linked, &invalid] {
                assert_eq!(store.entry(row.terminal_id), Some(row));
            }
        });
    }

    #[test]
    fn test_terminal_title_prefix_preserves_non_alphanumeric_prefixes() {
        assert_eq!(terminal_title_prefix("✳ Thinking"), Some("✳ "));
        assert_eq!(terminal_title_prefix(">>>   Thinking"), Some(">>>   "));
        assert_eq!(terminal_title_prefix("⠋ Running"), Some("⠋ "));
        assert_eq!(terminal_title_prefix("* Claude"), Some("* "));
        assert_eq!(terminal_title_prefix("✳Thinking"), None);
        assert_eq!(terminal_title_prefix("Thinking"), None);
        assert_eq!(terminal_title_prefix(" Thinking"), None);
        assert_eq!(terminal_title_prefix("✳"), None);
        assert_eq!(terminal_title_prefix("v1 Running"), None);
    }

    #[test]
    fn test_terminal_thread_display_title_combines_raw_and_custom_titles() {
        let mut metadata = metadata(
            "⠋ Thinking",
            WorktreePaths::from_folder_paths(&PathList::default()),
        );
        metadata.custom_title = Some("Fix bug".into());
        assert_eq!(metadata.display_title().as_ref(), "⠋ Fix bug");

        metadata.title = "Thinking".into();
        assert_eq!(metadata.display_title().as_ref(), "Fix bug");
    }

    #[gpui::test]
    async fn test_rename_terminal_updates_stored_custom_title(cx: &mut TestAppContext) {
        init_test(cx);

        let metadata = metadata(
            "⠋ Dev Server",
            WorktreePaths::from_folder_paths(&PathList::default()),
        );
        let terminal_id = metadata.terminal_id;

        cx.update(|cx| {
            TerminalThreadMetadataStore::global(cx).update(cx, |store, cx| {
                store.save(metadata, cx);
                store.rename_terminal(terminal_id, "Renamed Terminal".into(), cx);
            });
        });

        cx.update(|cx| {
            let store = TerminalThreadMetadataStore::global(cx);
            let metadata = store
                .read(cx)
                .entry(terminal_id)
                .expect("renamed terminal metadata should exist");
            assert_eq!(metadata.custom_title.as_deref(), Some("Renamed Terminal"));
            assert_eq!(metadata.display_title().as_ref(), "⠋ Renamed Terminal");
        });

        cx.update(|cx| {
            TerminalThreadMetadataStore::global(cx).update(cx, |store, cx| {
                store.rename_terminal(terminal_id, "Dev Server".into(), cx);
            });
            let store = TerminalThreadMetadataStore::global(cx);
            let metadata = store
                .read(cx)
                .entry(terminal_id)
                .expect("renamed terminal metadata should exist");
            assert_eq!(metadata.custom_title, None);
            assert_eq!(metadata.display_title().as_ref(), "⠋ Dev Server");
        });
    }

    #[gpui::test]
    async fn test_change_worktree_paths_reindexes_terminal_metadata(cx: &mut TestAppContext) {
        init_test(cx);

        let old_main_paths = PathList::new(&[Path::new("/repo")]);
        let old_folder_paths = PathList::new(&[Path::new("/repo-feature")]);
        let new_main_path = Path::new("/repo");
        let new_folder_path = Path::new("/repo-feature-renamed");
        let new_folder_paths = PathList::new(&[new_folder_path]);
        let metadata = metadata(
            "Dev Server",
            WorktreePaths::from_path_lists(old_main_paths.clone(), old_folder_paths.clone())
                .unwrap(),
        );
        let terminal_id = metadata.terminal_id;

        cx.update(|cx| {
            TerminalThreadMetadataStore::global(cx).update(cx, |store, cx| {
                store.save(metadata, cx);
            });
        });

        cx.update(|cx| {
            TerminalThreadMetadataStore::global(cx).update(cx, |store, cx| {
                store.change_worktree_paths(
                    &old_folder_paths,
                    None,
                    |paths| {
                        paths.add_path(new_main_path, new_folder_path);
                        paths.remove_folder_path(Path::new("/repo-feature"));
                    },
                    cx,
                );
            });
        });

        cx.update(|cx| {
            let store = TerminalThreadMetadataStore::global(cx);
            let store = store.read(cx);
            assert!(
                store
                    .entries_for_path(&old_folder_paths, None)
                    .next()
                    .is_none()
            );
            assert_eq!(
                store
                    .entries_for_path(&new_folder_paths, None)
                    .map(|entry| entry.terminal_id)
                    .collect::<Vec<_>>(),
                vec![terminal_id]
            );
            assert_eq!(
                store
                    .entry(terminal_id)
                    .unwrap()
                    .main_worktree_paths()
                    .paths(),
                old_main_paths.paths()
            );
        });
    }

    #[gpui::test]
    async fn test_created_at_immutable_on_resave(cx: &mut TestAppContext) {
        init_test(cx);

        let initial_time = DateTime::parse_from_rfc3339("2026-01-01T00:00:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let mut meta = metadata(
            "Test",
            WorktreePaths::from_folder_paths(&PathList::default()),
        );
        meta.created_at = initial_time;
        let terminal_id = meta.terminal_id;

        cx.update(|cx| {
            TerminalThreadMetadataStore::global(cx).update(cx, |store, cx| {
                store.save(meta.clone(), cx);
            });
        });
        cx.run_until_parked();

        let new_time = DateTime::parse_from_rfc3339("2026-02-01T00:00:00Z")
            .unwrap()
            .with_timezone(&Utc);
        meta.created_at = new_time;
        cx.update(|cx| {
            TerminalThreadMetadataStore::global(cx).update(cx, |store, cx| {
                store.save(meta, cx);
            });
        });
        cx.run_until_parked();

        cx.update(|cx| {
            let store = TerminalThreadMetadataStore::global(cx);
            let entry = store.read(cx).entry(terminal_id).unwrap().clone();
            assert_eq!(
                entry.created_at, initial_time,
                "in-memory created_at must be immutable"
            );
        });
    }

    #[gpui::test]
    async fn test_worktree_paths_not_overwritten_by_empty(cx: &mut TestAppContext) {
        init_test(cx);

        let paths = PathList::new(&[Path::new("/project")]);
        let worktree_paths = WorktreePaths::from_folder_paths(&paths);
        let mut meta = metadata("Test", worktree_paths.clone());
        let terminal_id = meta.terminal_id;

        cx.update(|cx| {
            TerminalThreadMetadataStore::global(cx).update(cx, |store, cx| {
                store.save(meta.clone(), cx);
            });
        });
        cx.run_until_parked();

        meta.worktree_paths = WorktreePaths::default();
        cx.update(|cx| {
            TerminalThreadMetadataStore::global(cx).update(cx, |store, cx| {
                store.save(meta, cx);
            });
        });
        cx.run_until_parked();

        cx.update(|cx| {
            let store = TerminalThreadMetadataStore::global(cx);
            let entry = store.read(cx).entry(terminal_id).unwrap().clone();
            assert_eq!(
                entry.worktree_paths, worktree_paths,
                "worktree paths must not be overwritten by empty"
            );
        });
    }

    #[gpui::test]
    async fn test_reload_preserves_concurrent_writes_and_honors_deleted_tombstones(
        cx: &mut TestAppContext,
    ) {
        init_test(cx);

        let database = cx.update(|cx| {
            let store = TerminalThreadMetadataStore::global(cx);
            store.read(cx).db.clone()
        });
        let deleted_metadata = metadata("Deleted", WorktreePaths::default());
        let old_folder_paths = PathList::new(&[Path::new("/project/old")]);
        let old_worktree_paths = WorktreePaths::from_folder_paths(&old_folder_paths);
        let old_metadata = metadata("Old value", old_worktree_paths.clone());
        let deleted_terminal_id = deleted_metadata.terminal_id;
        let replaced_terminal_id = old_metadata.terminal_id;
        database
            .save(deleted_metadata)
            .await
            .expect("deleted metadata should seed the database");
        database
            .save(old_metadata.clone())
            .await
            .expect("old metadata should seed the database");

        let new_folder_paths = PathList::new(&[Path::new("/project/new")]);
        let new_worktree_paths = WorktreePaths::from_folder_paths(&new_folder_paths);
        let mut replacement_metadata = old_metadata;
        replacement_metadata.title = "New value".into();
        replacement_metadata.worktree_paths = new_worktree_paths.clone();
        let added_metadata = metadata("Added during reload", new_worktree_paths.clone());
        let added_terminal_id = added_metadata.terminal_id;

        let (pending_terminal_ops_tx, pending_terminal_ops_rx) = async_channel::unbounded();
        let store = cx.update(|cx| {
            cx.new(|_| TerminalThreadMetadataStore {
                db: database.clone(),
                terminals: HashMap::default(),
                terminals_by_paths: HashMap::default(),
                terminals_by_main_paths: HashMap::default(),
                reload_task: None,
                load_error: None,
                pending_terminal_ops_tx,
                _db_operations_task: Task::ready(()),
                deleted_terminal_ids: HashSet::default(),
                closing_terminal_ids: HashSet::default(),
                intentionally_closed_terminal_ids: HashSet::default(),
            })
        });
        let _held_operations_receiver = pending_terminal_ops_rx;

        store.update(cx, |store, cx| store.reload(cx));
        let reload_task = store.read_with(cx, |store, _cx| store.reload_task());
        store.update(cx, |store, cx| {
            store.delete(deleted_terminal_id, cx);
            store.save(replacement_metadata.clone(), cx);
            store.save(added_metadata.clone(), cx);
        });

        reload_task.await;
        cx.run_until_parked();

        store.read_with(cx, |store, _cx| {
            assert!(
                store.entry(deleted_terminal_id).is_none(),
                "deleted database row must not return during reload"
            );
            let replacement = store
                .entry(replaced_terminal_id)
                .expect("replacement metadata should remain in the store");
            assert_eq!(replacement.title.as_ref(), "New value");
            assert_eq!(
                replacement.worktree_paths,
                replacement_metadata.worktree_paths
            );
            assert!(store.entry(added_terminal_id).is_some());
            assert!(
                store
                    .entries_for_path(&old_folder_paths, None)
                    .next()
                    .is_none(),
                "replaced metadata must leave the old path index"
            );
            let new_path_terminal_ids = store
                .entries_for_path(&new_folder_paths, None)
                .map(|metadata| metadata.terminal_id)
                .collect::<HashSet<_>>();
            assert_eq!(
                new_path_terminal_ids,
                [replaced_terminal_id, added_terminal_id]
                    .into_iter()
                    .collect::<HashSet<_>>()
            );
            let new_main_path_terminal_ids = store
                .entries_for_main_worktree_path(new_worktree_paths.main_worktree_path_list(), None)
                .map(|metadata| metadata.terminal_id)
                .collect::<HashSet<_>>();
            assert_eq!(
                new_main_path_terminal_ids,
                [replaced_terminal_id, added_terminal_id]
                    .into_iter()
                    .collect::<HashSet<_>>()
            );
            assert!(
                store.deleted_terminal_ids.is_empty(),
                "reload completion should clear deleted tombstones"
            );
            assert!(
                store.reload_task.is_none(),
                "completed reload should clear the in-flight task"
            );
        });

        store.update(cx, |store, cx| store.delete(added_terminal_id, cx));
        store.read_with(cx, |store, _cx| {
            assert!(
                !store.deleted_terminal_ids.contains(&added_terminal_id),
                "a delete after reload should not create a tombstone"
            );
        });
    }

    #[gpui::test]
    async fn test_update_worktree_paths_for_project_scopes_adoption_to_working_directory(
        cx: &mut TestAppContext,
    ) {
        init_test(cx);

        let remote_connection = RemoteConnectionOptions::Ssh(remote::SshConnectionOptions {
            host: "same-host".into(),
            ..Default::default()
        });
        let other_remote_connection = RemoteConnectionOptions::Ssh(remote::SshConnectionOptions {
            host: "other-host".into(),
            ..Default::default()
        });
        let project_folder_paths = PathList::new(&[Path::new("/repo")]);
        let project_worktree_paths = WorktreePaths::from_folder_paths(&project_folder_paths);
        let existing_folder_paths = PathList::new(&[Path::new("/existing")]);
        let existing_worktree_paths = WorktreePaths::from_folder_paths(&existing_folder_paths);

        let mut nested_metadata = metadata("Nested", WorktreePaths::default());
        nested_metadata.working_directory = Some(PathBuf::from("/repo/nested"));
        nested_metadata.remote_connection = Some(remote_connection.clone());
        let nested_terminal_id = nested_metadata.terminal_id;

        let mut root_metadata = metadata("Root", WorktreePaths::default());
        root_metadata.working_directory = Some(PathBuf::from("/repo"));
        root_metadata.remote_connection = Some(remote_connection.clone());
        let root_terminal_id = root_metadata.terminal_id;

        let mut sibling_metadata = metadata("Sibling prefix", WorktreePaths::default());
        sibling_metadata.working_directory = Some(PathBuf::from("/repo-other/nested"));
        sibling_metadata.remote_connection = Some(remote_connection.clone());
        let sibling_terminal_id = sibling_metadata.terminal_id;

        let mut other_host_metadata = metadata("Other host", WorktreePaths::default());
        other_host_metadata.working_directory = Some(PathBuf::from("/repo/nested"));
        other_host_metadata.remote_connection = Some(other_remote_connection);
        let other_host_terminal_id = other_host_metadata.terminal_id;

        let mut no_directory_metadata = metadata("No directory", WorktreePaths::default());
        no_directory_metadata.remote_connection = Some(remote_connection.clone());
        let no_directory_terminal_id = no_directory_metadata.terminal_id;

        let mut existing_metadata = metadata("Existing paths", existing_worktree_paths.clone());
        existing_metadata.working_directory = Some(PathBuf::from("/repo/nested"));
        existing_metadata.remote_connection = Some(remote_connection.clone());
        let existing_terminal_id = existing_metadata.terminal_id;

        cx.update(|cx| {
            TerminalThreadMetadataStore::global(cx).update(cx, |store, cx| {
                store.save(nested_metadata, cx);
                store.save(root_metadata, cx);
                store.save(sibling_metadata, cx);
                store.save(other_host_metadata, cx);
                store.save(no_directory_metadata, cx);
                store.save(existing_metadata, cx);
            });
        });
        cx.run_until_parked();

        cx.update(|cx| {
            TerminalThreadMetadataStore::global(cx).update(cx, |store, cx| {
                store.update_worktree_paths_for_project(
                    &project_worktree_paths,
                    PathStyle::local(),
                    Some(&remote_connection),
                    cx,
                );
            });
        });

        cx.update(|cx| {
            let store_entity = TerminalThreadMetadataStore::global(cx);
            let store = store_entity.read(cx);
            assert_eq!(
                store
                    .entry(nested_terminal_id)
                    .expect("nested directory metadata should remain")
                    .worktree_paths,
                project_worktree_paths
            );
            assert_eq!(
                store
                    .entry(root_terminal_id)
                    .expect("root directory metadata should remain")
                    .worktree_paths,
                project_worktree_paths
            );
            assert!(
                store
                    .entry(sibling_terminal_id)
                    .expect("sibling metadata should remain")
                    .worktree_paths
                    .is_empty(),
                "a sibling path with a shared string prefix must not be adopted"
            );
            assert!(
                store
                    .entry(other_host_terminal_id)
                    .expect("other-host metadata should remain")
                    .worktree_paths
                    .is_empty(),
                "paths from another remote identity must not be adopted"
            );
            assert!(
                store
                    .entry(no_directory_terminal_id)
                    .expect("metadata without a working directory should remain")
                    .worktree_paths
                    .is_empty(),
                "missing working directory is insufficient evidence for adoption"
            );
            assert_eq!(
                store
                    .entry(existing_terminal_id)
                    .expect("metadata with existing paths should remain")
                    .worktree_paths,
                existing_worktree_paths
            );
            let indexed_terminal_ids = store
                .entries_for_path(&project_folder_paths, Some(&remote_connection))
                .map(|metadata| metadata.terminal_id)
                .collect::<HashSet<_>>();
            assert_eq!(
                indexed_terminal_ids,
                [nested_terminal_id, root_terminal_id]
                    .into_iter()
                    .collect::<HashSet<_>>()
            );
            let indexed_main_worktree_ids = store
                .entries_for_main_worktree_path(
                    project_worktree_paths.main_worktree_path_list(),
                    Some(&remote_connection),
                )
                .map(|metadata| metadata.terminal_id)
                .collect::<HashSet<_>>();
            assert_eq!(
                indexed_main_worktree_ids,
                [nested_terminal_id, root_terminal_id]
                    .into_iter()
                    .collect::<HashSet<_>>()
            );
        });
    }

    #[gpui::test]
    async fn test_remote_windows_working_directory_uses_project_path_style(
        cx: &mut TestAppContext,
    ) {
        init_test(cx);

        let remote_connection = RemoteConnectionOptions::Ssh(remote::SshConnectionOptions {
            host: "windows-host".into(),
            ..Default::default()
        });
        let project_paths = PathList::new(&[Path::new(r"C:\repo")]);
        let project_worktree_paths = WorktreePaths::from_folder_paths(&project_paths);

        let mut nested = metadata("Windows nested", WorktreePaths::default());
        nested.working_directory = Some(PathBuf::from(r"C:\repo\nested"));
        nested.remote_connection = Some(remote_connection.clone());
        let nested_id = nested.terminal_id;

        let mut sibling = metadata("Windows sibling prefix", WorktreePaths::default());
        sibling.working_directory = Some(PathBuf::from(r"C:\repo-other\nested"));
        sibling.remote_connection = Some(remote_connection.clone());
        let sibling_id = sibling.terminal_id;

        cx.update(|cx| {
            TerminalThreadMetadataStore::global(cx).update(cx, |store, cx| {
                store.save(nested, cx);
                store.save(sibling, cx);
                store.update_worktree_paths_for_project(
                    &project_worktree_paths,
                    PathStyle::Windows,
                    Some(&remote_connection),
                    cx,
                );
            });
        });

        cx.update(|cx| {
            let store = TerminalThreadMetadataStore::global(cx);
            let store = store.read(cx);
            assert_eq!(
                store.entry(nested_id).unwrap().worktree_paths,
                project_worktree_paths,
                "a Windows remote cwd must be compared with the remote project's path style"
            );
            assert!(
                store.entry(sibling_id).unwrap().worktree_paths.is_empty(),
                "a Windows sibling path with a shared string prefix must not be adopted"
            );
        });
    }

    #[gpui::test]
    async fn test_ssh_routing_arguments_scope_terminal_project_assignment(
        cx: &mut TestAppContext,
    ) {
        init_test(cx);

        let connection = |configuration: &str| {
            RemoteConnectionOptions::Ssh(remote::SshConnectionOptions {
                host: "same-host".into(),
                username: Some("developer".into()),
                port: Some(22),
                args: Some(vec!["-F".into(), configuration.into()]),
                ..Default::default()
            })
        };
        let project_connection = connection("work.conf");
        let other_connection = connection("personal.conf");
        let project_paths = PathList::new(&[Path::new("/repo")]);
        let project_worktree_paths = WorktreePaths::from_folder_paths(&project_paths);

        let mut matching = metadata("Matching", WorktreePaths::default());
        matching.working_directory = Some(PathBuf::from("/repo"));
        matching.remote_connection = Some(project_connection.clone());
        let matching_id = matching.terminal_id;

        let mut other_destination = metadata("Other destination", WorktreePaths::default());
        other_destination.working_directory = Some(PathBuf::from("/repo"));
        other_destination.remote_connection = Some(other_connection.clone());
        let other_destination_id = other_destination.terminal_id;

        cx.update(|cx| {
            TerminalThreadMetadataStore::global(cx).update(cx, |store, cx| {
                store.save(matching, cx);
                store.save(other_destination, cx);
                store.update_worktree_paths_for_project(
                    &project_worktree_paths,
                    PathStyle::local(),
                    Some(&project_connection),
                    cx,
                );
            });
        });

        cx.update(|cx| {
            let store = TerminalThreadMetadataStore::global(cx);
            let store = store.read(cx);
            assert_eq!(
                store.entry(matching_id).unwrap().worktree_paths,
                project_worktree_paths
            );
            assert!(
                store
                    .entry(other_destination_id)
                    .unwrap()
                    .worktree_paths
                    .is_empty(),
                "an SSH destination with different routing arguments must not be assigned to the project"
            );
            assert_eq!(
                store
                    .entries_for_path(&project_paths, Some(&project_connection))
                    .map(|entry| entry.terminal_id)
                    .collect::<Vec<_>>(),
                vec![matching_id],
                "grouping and activation lookups must exclude the other SSH destination"
            );
            assert!(
                !store.path_is_referenced_by_terminal(
                    None,
                    Path::new("/repo"),
                    Some(&other_connection),
                ),
                "archive cleanup must not treat another SSH destination as a path reference"
            );
        });
    }

    #[gpui::test]
    async fn test_terminal_id_lookup_requires_matching_ssh_routing_arguments(
        cx: &mut TestAppContext,
    ) {
        init_test(cx);
        let connection = |configuration: &str| {
            RemoteConnectionOptions::Ssh(remote::SshConnectionOptions {
                host: "same-host".into(),
                username: Some("developer".into()),
                port: Some(22),
                args: Some(vec!["-F".into(), configuration.into()]),
                ..Default::default()
            })
        };
        let saved_connection = connection("work.conf");
        let current_connection = connection("personal.conf");
        assert!(same_remote_connection_identity(
            Some(&saved_connection),
            Some(&current_connection),
        ));

        let mut saved = metadata("Saved remote terminal", WorktreePaths::default());
        saved.remote_connection = Some(saved_connection.clone());
        let saved_id = saved.terminal_id;
        let local = metadata("Local terminal", WorktreePaths::default());
        let local_id = local.terminal_id;
        cx.update(|cx| {
            TerminalThreadMetadataStore::global(cx).update(cx, |store, cx| {
                store.save(saved, cx);
                store.save(local, cx);
            });
        });
        cx.update(|cx| {
            let store = TerminalThreadMetadataStore::global(cx);
            let store = store.read(cx);
            let original = store.entry(saved_id).cloned();
            assert!(original.is_some());
            assert_eq!(
                store.entry_for_remote_connection(saved_id, Some(&saved_connection)),
                original.as_ref(),
            );
            assert!(
                store
                    .entry_for_remote_connection(saved_id, Some(&current_connection))
                    .is_none(),
                "the same SSH alias with different -F arguments must not restore a saved terminal"
            );
            assert!(store.entry_for_remote_connection(saved_id, None).is_none());
            assert!(store.entry_for_remote_connection(local_id, None).is_some());
            assert!(
                store
                    .entry_for_remote_connection(local_id, Some(&saved_connection))
                    .is_none()
            );
            assert_eq!(store.entry(saved_id), original.as_ref());
        });
    }

    #[test]
    fn test_terminal_remote_identity_normalizes_empty_arguments() {
        let without = RemoteConnectionOptions::Ssh(remote::SshConnectionOptions {
            host: "example.com".into(), args: None, ..Default::default()
        });
        let mut empty = without.clone();
        if let RemoteConnectionOptions::Ssh(options) = &mut empty { options.args = Some(Vec::new()); }
        assert!(same_terminal_remote_connection(Some(&without), Some(&empty)));
        if let RemoteConnectionOptions::Ssh(options) = &mut empty {
            options.args = Some(vec!["-F".into(), "other.conf".into()]);
        }
        assert!(!same_terminal_remote_connection(Some(&without), Some(&empty)));
    }

}
