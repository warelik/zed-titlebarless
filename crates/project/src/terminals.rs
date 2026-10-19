use anyhow::{Context as _, Result};
use collections::HashMap;
use gpui::{App, AppContext as _, Context, Entity, Task, WeakEntity};

use futures::{FutureExt, future::Shared};
use itertools::Itertools as _;
use language::LanguageName;
use remote::{Interactive, RemoteClient};
use rpc::proto;
use settings::{Settings, SettingsLocation};
use std::{
    borrow::Cow,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use task::{Shell, ShellBuilder, ShellKind, SpawnInTerminal};
use terminal::{
    PtyStartup, Terminal, TerminalBounds, TerminalBuilder, TerminalMode, insert_zed_terminal_env,
    terminal_settings::TerminalSettings,
};
use util::{
    command::new_std_command, get_default_system_shell, get_system_shell, maybe, rel_path::RelPath,
};

use crate::{Project, ProjectPath};

pub struct Terminals {
    pub(crate) local_handles: Vec<WeakEntity<terminal::Terminal>>,
    pub(crate) session_contexts: HashMap<String, ShpoolSessionContext>,
}

/// The locator of the daemon that actually owns a persistent session.
/// Never persist the shell environment: it may contain credentials.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ShpoolSessionContext {
    program: String,
    // This is a POSIX path on the daemon's host, even on a Windows client.
    socket: String,
}

impl ShpoolSessionContext {
    pub fn new(program: String, socket: PathBuf) -> Result<Self> {
        let context = Self { program, socket: socket.to_string_lossy().into_owned() };
        context.validate()?;
        Ok(context)
    }

    pub fn same_daemon(&self, other: &Self) -> bool {
        self.socket == other.socket
    }

    pub fn validate(&self) -> Result<()> {
        anyhow::ensure!(
            self.program.starts_with('/') && self.socket.starts_with('/'),
            "shpool context must contain absolute client and socket paths",
        );
        anyhow::ensure!(
            !self.program.contains('\0') && !self.socket.contains('\0'),
            "shpool context contains a NUL byte",
        );
        Ok(())
    }

    fn runtime_prefix(&self) -> Result<String> {
        // Keep the namespace of the native discovery recipe, independently of
        // the current project's XDG setting and the client's path conventions.
        match self.socket.strip_suffix("/shpool/shpool.socket") {
            Some(runtime) => {
                let runtime = if runtime.is_empty() { "/" } else { runtime };
                Ok(format!("export XDG_RUNTIME_DIR={}\n",
                    ShellKind::Posix.try_quote(runtime)
                        .context("cannot quote original shpool runtime directory")?))
            }
            None => Ok(String::new()),
        }
    }

    fn command(&self, operation: &str) -> Result<String> {
        self.validate()?;
        let program = ShellKind::Posix.try_quote(&self.program)
            .context("cannot quote original shpool client")?;
        let socket = ShellKind::Posix.try_quote(&self.socket)
            .context("cannot quote original shpool socket")?.into_owned();
        // -D forbids creating an empty replacement daemon and mistaking its
        // missing session for proof that the original process was terminated.
        Ok(format!("{}exec {program} -D -s {socket} {operation}\n", self.runtime_prefix()?))
    }

    fn probe_script(&self, allow_daemon_start: bool) -> Result<String> {
        self.validate()?;
        let program = ShellKind::Posix.try_quote(&self.program)
            .context("cannot quote original shpool client")?;
        let socket = ShellKind::Posix.try_quote(&self.socket)
            .context("cannot quote original shpool socket")?.into_owned();
        // Restore keeps upstream's daemon startup behavior, at the captured
        // address. Destructive operations alone forbid starting a replacement.
        Ok(format!(
            "{}printf '%s\n' '{SHPOOL_PROBE_MARKER}' {program} {socket}\nexec {program} {no_daemon}-s {socket} list --json\n",
            self.runtime_prefix()?,
            no_daemon = if allow_daemon_start { "" } else { "-D " },
        ))
    }

    #[cfg(any(test, feature = "test-support"))]
    pub fn for_test() -> Self {
        Self::new("/shpool".into(), "/tmp/fake-shpool.socket".into()).unwrap()
    }
}

pub struct PersistentTerminal {
    pub terminal: Entity<Terminal>,
    pub session: PersistentSession,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PersistentSession {
    /// The host has no usable shpool, so the terminal runs a plain shell.
    Unavailable,
    Created,
    Reattached,
}

/// A shpool attach request, resolved after the terminal's shell settings.
struct ShellCommand {
    program: String,
    socket: PathBuf,
    session_name: String,
    title_override: Option<String>,
}

#[cfg(any(test, feature = "test-support"))]
#[derive(Clone, Default)]
pub struct FakeShpoolStore {
    pub state: Arc<parking_lot::Mutex<FakeShpoolState>>,
}

#[cfg(any(test, feature = "test-support"))]
pub type FakeShpool = FakeShpoolStore;

#[cfg(any(test, feature = "test-support"))]
#[derive(Default)]
pub struct FakeShpoolState {
    pub available: bool,
    pub probe_error: Option<String>,
    pub probe_delay: Option<Duration>,
    pub program: String,
    pub socket: PathBuf,
    pub sessions: collections::HashSet<String>,
    pub attach_history: Vec<String>,
    pub attach_args_history: Vec<(String, Vec<String>)>,
    pub kill_history: Vec<String>,
    pub kill_error: Option<String>,
    pub kill_delay: Option<Duration>,
}

#[cfg(any(test, feature = "test-support"))]
impl FakeShpoolStore {
    pub fn new() -> Self {
        Self {
            state: Arc::new(parking_lot::Mutex::new(FakeShpoolState {
                available: true,
                program: "shpool".to_string(),
                socket: PathBuf::from("/tmp/fake-shpool.socket"),
                ..Default::default()
            })),
        }
    }

    pub fn with_sessions(sessions: impl IntoIterator<Item = String>) -> Self {
        let store = Self::new();
        store.state.lock().sessions = sessions.into_iter().collect();
        store
    }

    pub fn add_session(&self, name: impl Into<String>) {
        self.state.lock().sessions.insert(name.into());
    }

    pub fn remove_session(&self, name: &str) {
        self.state.lock().sessions.remove(name);
    }

    pub fn has_session(&self, name: &str) -> bool {
        self.state.lock().sessions.contains(name)
    }

    pub fn attach_history(&self) -> Vec<String> {
        self.state.lock().attach_history.clone()
    }

    pub fn attach_args_history(&self) -> Vec<(String, Vec<String>)> {
        self.state.lock().attach_args_history.clone()
    }

    pub fn kill_history(&self) -> Vec<String> {
        self.state.lock().kill_history.clone()
    }

    pub fn sessions(&self) -> collections::HashSet<String> {
        self.state.lock().sessions.clone()
    }
}

macro_rules! shpool_script_prelude {
    ($missing_client_status:literal) => {
        concat!(r#"
SHPOOL_BIN=""
for b in shpool "$HOME/.local/bin/shpool" "$HOME/.cargo/bin/shpool" /opt/homebrew/bin/shpool /usr/local/bin/shpool; do
    if command -v "$b" >/dev/null 2>&1; then
        SHPOOL_BIN="$(command -v "$b")"
        break
    elif [ -x "$b" ]; then
        SHPOOL_BIN="$b"
        break
    fi
done
[ -n "$SHPOOL_BIN" ] || exit "#, $missing_client_status, r#"
case "$SHPOOL_BIN" in
    /*) ;;
    *) SHPOOL_BIN="$PWD/$SHPOOL_BIN" ;;
esac

USER_ID="$(id -u 2>/dev/null || true)"

SHPOOL_SOCK=""
if [ -n "$XDG_RUNTIME_DIR" ]; then
    SHPOOL_SOCK="$XDG_RUNTIME_DIR/shpool/shpool.socket"
elif [ -n "$USER_ID" ] && [ -S "/run/user/$USER_ID/shpool/shpool.socket" ]; then
    SHPOOL_SOCK="/run/user/$USER_ID/shpool/shpool.socket"
elif [ -S "$HOME/.local/run/shpool/shpool.socket" ]; then
    SHPOOL_SOCK="$HOME/.local/run/shpool/shpool.socket"
elif [ -n "$USER_ID" ] && [ -d "/run/user/$USER_ID" ]; then
    SHPOOL_SOCK="/run/user/$USER_ID/shpool/shpool.socket"
else
    SHPOOL_SOCK="$HOME/.local/run/shpool/shpool.socket"
fi
"#)
    };
}

/// Prelude searching for shpool binary and socket path in script regressions.
#[cfg(test)]
const SHPOOL_SCRIPT_PRELUDE: &str = shpool_script_prelude!("0");

/// Prelude used for destructive operations, which must fail when shpool is unavailable.
const SHPOOL_KILL_SCRIPT_PRELUDE: &str = shpool_script_prelude!("127");

pub(crate) const SHPOOL_PROBE_MARKER: &str = "=== ZED_SHPOOL_PROBE_START ===";

/// Complete script probing shpool binary, live/fallback socket, and running sessions.
const SHPOOL_PROBE_SCRIPT: &str = concat!(
    shpool_script_prelude!("0"),
    r#"
echo "=== ZED_SHPOOL_PROBE_START ==="
echo "$SHPOOL_BIN"
echo "$SHPOOL_SOCK"
exec "$SHPOOL_BIN" -s "$SHPOOL_SOCK" list --json
"#
);

/// Legacy destructive operations may discover an existing daemon, never start
/// an unrelated empty one and infer that its absent session has ended.
const SHPOOL_EXISTING_PROBE_SCRIPT: &str = concat!(
    shpool_script_prelude!("127"),
    r#"
echo "=== ZED_SHPOOL_PROBE_START ==="
echo "$SHPOOL_BIN"
echo "$SHPOOL_SOCK"
exec "$SHPOOL_BIN" -D -s "$SHPOOL_SOCK" list --json
"#
);

pub(crate) const SHPOOL_PROBE_TIMEOUT: Duration = Duration::from_secs(17);
pub(crate) const SHPOOL_KILL_TIMEOUT: Duration = Duration::from_secs(17);

pub struct ShpoolProbe {
    pub program: String,
    pub socket: PathBuf,
    pub sessions: Vec<String>,
}

pub fn parse_shpool_probe(stdout: &str) -> Result<Option<ShpoolProbe>> {
    #[derive(serde::Deserialize)]
    struct SessionList {
        sessions: Vec<Session>,
    }
    #[derive(serde::Deserialize)]
    struct Session {
        name: String,
    }

    // If marker is absent, shpool binary was not found (script exited 0 before marker).
    let Some(marker_index) = stdout.find(SHPOOL_PROBE_MARKER) else {
        return Ok(None);
    };

    let probe_output = &stdout[marker_index + SHPOOL_PROBE_MARKER.len()..];
    let probe_output = probe_output.trim_start_matches(['\r', '\n']);

    let mut lines = probe_output.splitn(3, '\n');
    let program = lines
        .next()
        .map(|line| line.trim())
        .filter(|line| !line.is_empty())
        .ok_or_else(|| anyhow::anyhow!("shpool probe output missing program line"))?;
    let socket = lines
        .next()
        .map(|line| line.trim())
        .filter(|line| !line.is_empty())
        .ok_or_else(|| anyhow::anyhow!("shpool probe output missing socket line"))?;
    let sessions_json = lines
        .next()
        .map(|line| line.trim())
        .filter(|line| !line.is_empty())
        .ok_or_else(|| anyhow::anyhow!("shpool probe output missing session JSON"))?;

    if !socket.starts_with('/') {
        anyhow::bail!("shpool probe returned relative socket {socket:?}, rejecting");
    }
    let socket = PathBuf::from(socket);

    let sessions = serde_json::from_str::<SessionList>(sessions_json)
        .context("parsing `shpool list --json`")?;
    Ok(Some(ShpoolProbe {
        program: program.to_string(),
        socket,
        sessions: sessions
            .sessions
            .into_iter()
            .map(|session| session.name)
            .collect(),
    }))
}

pub(crate) fn parse_shpool_probe_output(
    output: &std::process::Output,
) -> Result<Option<ShpoolProbe>> {
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        log::warn!(
            "shpool probe script failed with status {}: {}",
            output.status,
            stderr.trim()
        );
        anyhow::bail!(
            "shpool probe script failed with status {}: {}",
            output.status,
            stderr.trim()
        );
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    parse_shpool_probe(&stdout)
}

fn shpool_shell_command(shell: &Shell, literal_cwd: Option<&Path>) -> Result<String> {
    let command = match shell {
        Shell::System => "/bin/sh -c 'exec \"$SHELL\" -l'".to_string(),
        _ => {
            let (program, args) = shell.program_and_args();
            let words = std::iter::once(program.as_str())
                .chain(args.iter().map(String::as_str))
                .map(|word| {
                    ShellKind::Posix
                        .try_quote(word)
                        .context("invalid terminal shell argument")
                })
                .collect::<Result<Vec<_>>>()?;
            words.join(" ")
        }
    };
    if literal_cwd.is_none() && !command.contains('{') && !command.contains('}') {
        return Ok(command);
    }

    // shpool templates have no literal-brace escape. Carry a POSIX-quoted
    // script as octal bytes; decode only in the spawned shell, not in shpool.
    let directory = literal_cwd
        .map(|cwd| -> Result<String> {
            let cwd = cwd.to_string_lossy();
            Ok(format!(
                "cd -- {} && ",
                ShellKind::Posix
                    .try_quote(&cwd)
                    .context("invalid terminal working directory")?
            ))
        })
        .transpose()?
        .unwrap_or_default();
    let script = format!("{directory}exec {command}");
    let encoded: String = script
        .bytes()
        .map(|byte| format!("\\0{byte:03o}"))
        .collect();
    let launcher = format!("exec /bin/sh -c \"$(printf '%b' '{encoded}')\"");
    Ok(format!(
        "/bin/sh -c {}",
        ShellKind::Posix
            .try_quote(&launcher)
            .context("invalid persistent shell launcher")?
    ))
}

pub(crate) fn build_shpool_attach_args(
    socket: &Path,
    cwd: Option<&Path>,
    session_name: &str,
    shell: &Shell,
) -> Result<Vec<String>> {
    let mut args = vec![
        "-s".to_string(),
        socket.to_string_lossy().into_owned(),
        "attach".to_string(),
        "--force".to_string(),
    ];
    let has_template = |path: &Path| {
        let value = path.to_string_lossy();
        value.contains('{') || value.contains('}')
    };
    if let Some(cwd) = cwd.filter(|path| !has_template(path)) {
        args.push("--dir".to_string());
        args.push(cwd.to_string_lossy().into_owned());
    }
    // Pass --cmd even on reattachment: dead listed sessions must respawn
    // with the configured shell rather than shpool's default command.
    args.push("--cmd".to_string());
    args.push(shpool_shell_command(
        shell,
        cwd.filter(|path| has_template(path)),
    )?);
    args.push(session_name.to_string());
    Ok(args)
}

impl Project {
    pub fn active_entry_directory(&self, cx: &App) -> Option<PathBuf> {
        let entry_id = self.active_entry()?;
        let worktree = self.worktree_for_entry(entry_id, cx)?;
        let worktree = worktree.read(cx);
        let entry = worktree.entry_for_id(entry_id)?;

        let absolute_path = worktree.absolutize(entry.path.as_ref());
        if entry.is_dir() {
            Some(absolute_path)
        } else {
            absolute_path.parent().map(|p| p.to_path_buf())
        }
    }

    pub fn active_project_directory(&self, cx: &App) -> Option<Arc<Path>> {
        self.active_entry()
            .and_then(|entry_id| self.worktree_for_entry(entry_id, cx))
            .into_iter()
            .chain(self.worktrees(cx))
            .find_map(|tree| tree.read(cx).root_dir())
    }

    pub fn first_project_directory(&self, cx: &App) -> Option<PathBuf> {
        let worktree = self.worktrees(cx).next()?;
        let worktree = worktree.read(cx);
        if worktree.root_entry()?.is_dir() {
            Some(worktree.abs_path().to_path_buf())
        } else {
            None
        }
    }

    pub fn create_terminal_task(
        &mut self,
        spawn_task: SpawnInTerminal,
        cx: &mut Context<Self>,
    ) -> Task<Result<Entity<Terminal>>> {
        let is_via_remote = self.remote_client.is_some();

        let path: Option<Arc<Path>> = if let Some(cwd) = &spawn_task.cwd {
            if is_via_remote {
                Some(Arc::from(cwd.as_ref()))
            } else {
                let cwd = cwd.to_string_lossy();
                let tilde_substituted = shellexpand::tilde(&cwd);
                Some(Arc::from(Path::new(tilde_substituted.as_ref())))
            }
        } else {
            self.active_project_directory(cx)
        };

        let mut settings_location = None;
        if let Some(path) = path.as_ref()
            && let Some((worktree, _)) = self.find_worktree(path, cx)
        {
            settings_location = Some(SettingsLocation {
                worktree_id: worktree.read(cx).id(),
                path: RelPath::empty(),
            });
        }
        let settings = TerminalSettings::get(settings_location, cx).clone();
        let detect_venv = settings.detect_venv.as_option().is_some();

        let terminal_mode = TerminalMode::task(spawn_task.clone());

        let local_path = if is_via_remote { None } else { path.clone() };
        let remote_client = self.remote_client.clone();
        let shell = match &remote_client {
            Some(remote_client) => remote_client
                .read(cx)
                .shell()
                .unwrap_or_else(get_default_system_shell),
            None => get_system_shell(),
        };
        let path_style = self.path_style(cx);
        let shell_kind = ShellKind::new(&shell, path_style.is_windows());

        // Prepare a task for resolving the environment
        let env_task =
            self.resolve_directory_environment(&shell, path.clone(), remote_client.clone(), cx);

        // Scope the toolchain lookup to the worktree the terminal is being
        // spawned in. Previously this iterated the active editor's worktree
        // and then every visible worktree, so a Python toolchain persisted
        // for worktree A would leak into a terminal opened in worktree B and
        // inject (e.g.) `conda activate base` into a shell that has no
        // business with conda.
        let project_path_contexts: Vec<ProjectPath> = path
            .as_ref()
            .and_then(|p| self.find_worktree(p, cx))
            .map(|(worktree, relative_path)| ProjectPath {
                worktree_id: worktree.read(cx).id(),
                path: relative_path,
            })
            .into_iter()
            .collect();
        let toolchains = project_path_contexts
            .into_iter()
            .filter(|_| detect_venv)
            .map(|p| self.active_toolchain(p, LanguageName::new_static("Python"), cx))
            .collect::<Vec<_>>();
        let lang_registry = self.languages.clone();
        cx.spawn(async move |project, cx| {
            let mut env = env_task.await.unwrap_or_default();
            env.extend(settings.env);

            let activation_script = maybe!(async {
                for toolchain in toolchains {
                    let Some(toolchain) = toolchain.await else {
                        continue;
                    };
                    let language = lang_registry
                        .language_for_name(&toolchain.language_name.0)
                        .await
                        .ok();
                    let lister = language?.toolchain_lister()?;
                    let future =
                        cx.update(|cx| lister.activation_script(&toolchain, shell_kind, cx));
                    return Some(future.await);
                }
                None
            })
            .await
            .unwrap_or_default();

            let builder = project
                .update(cx, move |_, cx| {
                    let format_to_run = |spawn_task: &SpawnInTerminal| {
                        format_task_for_activation(
                            spawn_task,
                            shell_kind,
                            &shell,
                            path_style.is_windows(),
                        )
                    };

                    let (shell, env) = {
                        let to_run =
                            (!activation_script.is_empty()).then(|| format_to_run(&spawn_task));
                        env.extend(spawn_task.env);
                        match remote_client {
                            Some(remote_client) => match activation_script.clone() {
                                activation_script if !activation_script.is_empty() => {
                                    let separator = shell_kind.sequential_commands_separator();
                                    let activation_script =
                                        activation_script.join(&format!("{separator} "));
                                    let to_run = to_run.expect("activation command was formatted");

                                    let arg = format!("{activation_script}{separator} {to_run}");
                                    let args = shell_kind.args_for_shell(true, arg);
                                    let shell = remote_client
                                        .read(cx)
                                        .shell()
                                        .unwrap_or_else(get_default_system_shell);

                                    create_remote_shell(
                                        Some((&shell, &args)),
                                        env,
                                        path,
                                        remote_client,
                                        cx,
                                    )?
                                }
                                _ => create_remote_shell(
                                    spawn_task
                                        .command
                                        .as_ref()
                                        .map(|command| (command, &spawn_task.args)),
                                    env,
                                    path,
                                    remote_client,
                                    cx,
                                )?,
                            },
                            None => match activation_script.clone() {
                                activation_script if !activation_script.is_empty() => {
                                    let separator = shell_kind.sequential_commands_separator();
                                    let activation_script =
                                        activation_script.join(&format!("{separator} "));
                                    let to_run = to_run.expect("activation command was formatted");

                                    let arg = format!("{activation_script}{separator} {to_run}");
                                    let args = shell_kind.args_for_shell(true, arg);

                                    (
                                        Shell::WithArguments {
                                            program: shell,
                                            args,
                                            title_override: None,
                                        },
                                        env,
                                    )
                                }
                                _ => (
                                    if let Some(program) = spawn_task.command {
                                        Shell::WithArguments {
                                            program,
                                            args: spawn_task.args,
                                            title_override: None,
                                        }
                                    } else {
                                        Shell::System
                                    },
                                    env,
                                ),
                            },
                        }
                    };
                    anyhow::Ok(TerminalBuilder::new(
                        local_path.map(|path| path.to_path_buf()),
                        terminal_mode,
                        shell,
                        env,
                        settings.cursor_shape,
                        settings.alternate_scroll,
                        settings.max_scroll_history_lines,
                        settings.path_hyperlink_regexes,
                        Duration::from_millis(settings.path_hyperlink_timeout_ms),
                        is_via_remote,
                        cx.entity_id().as_u64(),
                        cx,
                        activation_script,
                        path_style,
                        PtyStartup::default(),
                    ))
                })??
                .await?;
            project.update(cx, move |this, cx| {
                let terminal_handle = cx.new(|cx| builder.subscribe(cx));

                this.terminals
                    .local_handles
                    .push(terminal_handle.downgrade());

                let id = terminal_handle.entity_id();
                cx.observe_release(&terminal_handle, move |project, _terminal, cx| {
                    let handles = &mut project.terminals.local_handles;

                    if let Some(index) = handles
                        .iter()
                        .position(|terminal| terminal.entity_id() == id)
                    {
                        handles.remove(index);
                        cx.notify();
                    }
                })
                .detach();

                terminal_handle
            })
        })
    }

    pub fn create_terminal_shell(
        &mut self,
        cwd: Option<PathBuf>,
        cx: &mut Context<Self>,
    ) -> Task<Result<Entity<Terminal>>> {
        self.create_terminal_shell_internal(cwd, false, None, PtyStartup::default(), cx)
    }

    /// Creates a terminal whose shell lives in the shpool session `session_name`
    /// on the host the shell runs on, so the shell outlives the terminal, the
    /// SSH connection and Zed itself. Reattaches when the session exists.
    /// Without shpool on that host, the terminal runs a plain shell.
    /// `bounds` is the size the terminal will be laid out at, when known: shpool
    /// restores the session's screen at the size the terminal opens with.
    pub fn create_persistent_terminal_shell(
        &mut self,
        cwd: Option<PathBuf>,
        session_name: String,
        bounds: Option<TerminalBounds>,
        cx: &mut Context<Self>,
    ) -> Task<Result<PersistentTerminal>> {
        self.create_persistent_terminal_shell_in_context(
            cwd, session_name, bounds, None, false, cx,
        )
    }

    pub fn persistent_terminal_context(&self, session_name: &str) -> Option<&ShpoolSessionContext> {
        self.terminals.session_contexts.get(session_name)
    }

    fn context_for_probe(&self, probe: &ShpoolProbe) -> Result<ShpoolSessionContext> {
        let program = probe.program.clone();
        #[cfg(any(test, feature = "test-support"))]
        let program = if self.fake_shpool.is_some() && !program.starts_with('/') {
            // Keep the existing fake probe protocol (including its bare client
            // name), but model the absolute locator captured by a real probe.
            format!("/{program}")
        } else { program };
        ShpoolSessionContext::new(program, probe.socket.clone())
    }

    pub fn create_persistent_terminal_shell_in_context(
        &mut self,
        cwd: Option<PathBuf>,
        session_name: String,
        bounds: Option<TerminalBounds>,
        context: Option<ShpoolSessionContext>,
        restoring_existing: bool,
        cx: &mut Context<Self>,
    ) -> Task<Result<PersistentTerminal>> {
        let known = self.terminals.session_contexts.get(&session_name).cloned();
        if context.as_ref().zip(known.as_ref()).is_some_and(|(left, right)| !left.same_daemon(right)) {
            return Task::ready(Err(anyhow::anyhow!("persistent session daemon context changed")));
        }
        let context = context.or(known);
        let legacy_restore = restoring_existing && context.is_none();
        let pinned = context.is_some();
        let probe = match context {
            Some(context) => self.probe_shpool_in_context(Some(context), true, cx),
            None if legacy_restore => self.probe_shpool_in_context(None, false, cx),
            None => self.probe_shpool(cx),
        };
        let is_local = self.remote_client.is_none();
        cx.spawn(async move |project, cx| {
            let probe_result = probe.await?;
            if legacy_restore {
                anyhow::ensure!(
                    probe_result.as_ref().is_some_and(|probe| probe.sessions.contains(&session_name)),
                    "original shpool context is unknown and the saved session was not found; metadata retained",
                );
            }
            anyhow::ensure!(!pinned || probe_result.is_some(), "original shpool daemon is unavailable");
            let context = match &probe_result {
                Some(probe) => Some(project.update(cx, |project, _| project.context_for_probe(probe))??),
                None => None,
            };
            let (command, session) = match probe_result {
                Some(shpool) => {
                    let session = if shpool.sessions.contains(&session_name) {
                        PersistentSession::Reattached
                    } else {
                        PersistentSession::Created
                    };
                    // The foreground process of a local terminal is now the shpool
                    // client, so a title built from it would name shpool, not the
                    // program in the session. Remote shells get the same override.
                    let title_override = is_local.then(|| {
                        let directory = cwd
                            .as_ref()
                            .and_then(|cwd| cwd.file_name())
                            .map(|name| name.to_string_lossy().into_owned());
                        match directory {
                            Some(directory) => format!("{directory} — Terminal"),
                            None => "Terminal".to_string(),
                        }
                    });
                    (
                        Some(ShellCommand {
                            program: shpool.program,
                            socket: shpool.socket,
                            session_name: session_name.clone(),
                            title_override,
                        }),
                        session,
                    )
                }
                None => (None, PersistentSession::Unavailable),
            };
            let pty_startup = PtyStartup {
                bounds,
                hold_resize_until_output: command.is_some(),
            };
            let terminal = project
                .update(cx, |project, cx| {
                    #[cfg(any(test, feature = "test-support"))]
                    if let Some(fake) = &project.fake_shpool {
                        let location = project.terminal_settings_location(cwd.as_deref(), cx);
                        let settings = TerminalSettings::get(location, cx).clone();
                        if let Some(command) = &command {
                            let args = match build_shpool_attach_args(
                                &command.socket,
                                cwd.as_deref(),
                                &command.session_name,
                                &settings.shell,
                            ) {
                                Ok(args) => args,
                                Err(error) => return Task::ready(Err(error)),
                            };
                            let mut state = fake.state.lock();
                            state.attach_history.push(command.session_name.clone());
                            state
                                .attach_args_history
                                .push((command.session_name.clone(), args));
                            state.sessions.insert(command.session_name.clone());
                        }
                        let path_style = project.path_style(cx);
                        let background_executor = cx.background_executor().clone();
                        let builder = terminal::TerminalBuilder::new_display_only_with_bounds(
                            settings.cursor_shape,
                            settings.alternate_scroll,
                            settings.max_scroll_history_lines,
                            cx.entity_id().as_u64(),
                            &background_executor,
                            path_style,
                            bounds.unwrap_or_default(),
                        );
                        let terminal = cx.new(|cx| builder.subscribe(cx));
                        project.terminals.local_handles.push(terminal.downgrade());
                        return Task::ready(Ok(terminal));
                    }

                    project.create_terminal_shell_internal(cwd, false, command, pty_startup, cx)
                })?
                .await?;
            if let Some(context) = context {
                project.update(cx, |project, _| {
                    anyhow::ensure!(
                        project.terminals.session_contexts.get(&session_name)
                            .is_none_or(|known| known.same_daemon(&context)),
                        "persistent session daemon context changed during startup",
                    );
                    project.terminals.session_contexts.entry(session_name).or_insert(context);
                    anyhow::Ok(())
                })??;
            }
            Ok(PersistentTerminal { terminal, session })
        })
    }

    fn shpool_session_not_found(stderr: &str, session_name: &str) -> bool {
        let diagnostics = [
            format!("not found: {session_name}"),
            format!("no such session: {session_name}"),
        ];
        stderr.lines().any(|line| {
            let line = line.trim();
            diagnostics
                .iter()
                .any(|diagnostic| line == diagnostic || line == format!("Error: {diagnostic}"))
        })
    }

    #[cfg(any(test, feature = "test-support"))]
    pub fn ensure_test_persistent_terminal_context(
        &mut self,
        session_name: String,
    ) -> Result<ShpoolSessionContext> {
        if let Some(context) = self.terminals.session_contexts.get(&session_name) {
            return Ok(context.clone());
        }
        let context = match &self.fake_shpool {
            Some(fake) => {
                let state = fake.state.lock();
                self.context_for_probe(&ShpoolProbe {
                    program: state.program.clone(), socket: state.socket.clone(), sessions: Vec::new(),
                })?
            }
            None => ShpoolSessionContext::for_test(),
        };
        self.terminals.session_contexts.insert(session_name, context.clone());
        Ok(context)
    }

    /// Ends the shpool session `session_name` and everything running in it.
    /// Unavailable or timed-out kills return an error so the close can be retried.
    pub fn kill_persistent_terminal_session(
        &mut self,
        session_name: String,
        cx: &mut Context<Self>,
    ) -> Task<Result<()>> {
        self.kill_persistent_terminal_session_in_context_inner(session_name, None, false, cx)
    }

    /// UI metadata without a locator may only be adopted after a positive
    /// session-list match. Absence in a newly selected daemon proves nothing.
    pub fn kill_persistent_terminal_session_in_context(
        &mut self,
        session_name: String,
        context: Option<ShpoolSessionContext>,
        cx: &mut Context<Self>,
    ) -> Task<Result<()>> {
        self.kill_persistent_terminal_session_in_context_inner(session_name, context, true, cx)
    }

    fn kill_persistent_terminal_session_in_context_inner(
        &mut self,
        session_name: String,
        context: Option<ShpoolSessionContext>,
        require_legacy_match: bool,
        cx: &mut Context<Self>,
    ) -> Task<Result<()>> {
        let known = self.terminals.session_contexts.get(&session_name).cloned();
        if context.as_ref().zip(known.as_ref()).is_some_and(|(left, right)| !left.same_daemon(right)) {
            return Task::ready(Err(anyhow::anyhow!("persistent session daemon context changed")));
        }
        let context = context.or(known);
        if let Some(context) = &context {
            if let Err(error) = context.validate() { return Task::ready(Err(error)); }
            self.terminals.session_contexts.entry(session_name.clone())
                .or_insert_with(|| context.clone());
        }
        let kill = if context.is_none() && require_legacy_match {
            let probe = self.probe_shpool_in_context(None, false, cx);
            let name = session_name.clone();
            cx.spawn(async move |project, cx| {
                let probe = probe.await?.context("original shpool daemon is unavailable")?;
                anyhow::ensure!(probe.sessions.contains(&name),
                    "saved session was not found in the current daemon; original context is unknown");
                let context = project.update(cx, |project, _| project.context_for_probe(&probe))??;
                let kill = project.update(cx, |project, cx| {
                    anyhow::ensure!(
                        project.terminals.session_contexts.get(&name)
                            .is_none_or(|known| known.same_daemon(&context)),
                        "original daemon changed while locating the legacy session",
                    );
                    project.terminals.session_contexts.entry(name.clone()).or_insert_with(|| context.clone());
                    anyhow::Ok(project.kill_persistent_terminal_session_unbounded(name, Some(context), cx))
                })??;
                kill.await
            })
        } else {
            self.kill_persistent_terminal_session_unbounded(session_name.clone(), context, cx)
        };
        let executor = cx.background_executor().clone();
        cx.background_spawn(async move {
            let timeout = executor.timer(SHPOOL_KILL_TIMEOUT);
            futures::pin_mut!(kill, timeout);
            match futures::future::select(kill, timeout).await {
                futures::future::Either::Left((result, _)) => result,
                futures::future::Either::Right(((), _)) => Err(anyhow::anyhow!(
                    "shpool session {session_name} kill timed out after {}s",
                    SHPOOL_KILL_TIMEOUT.as_secs()
                )),
            }
        })
    }

    /// The UI releases this cache after its intentional close succeeds. The
    /// low-level API retains it so repeated kills remain on the same daemon.
    pub fn forget_persistent_terminal_context(&mut self, session_name: &str) {
        self.terminals.session_contexts.remove(session_name);
    }

    fn kill_persistent_terminal_session_unbounded(
        &mut self,
        session_name: String,
        context: Option<ShpoolSessionContext>,
        cx: &mut Context<Self>,
    ) -> Task<Result<()>> {
        if let Some(context) = &context {
            if let Err(error) = context.validate() { return Task::ready(Err(error)); }
        }
        #[cfg(any(test, feature = "test-support"))]
        if let Some(fake) = &self.fake_shpool {
            if context.as_ref().is_some_and(|context| {
                let state = fake.state.lock();
                !state.available || state.socket.to_string_lossy().as_bytes() != context.socket.as_bytes()
            }) {
                return Task::ready(Err(anyhow::anyhow!("original shpool daemon is unavailable")));
            }
            let state = fake.state.clone();
            let delay = {
                let mut state = state.lock();
                state.kill_history.push(session_name.clone());
                state.kill_delay
            };
            if let Some(delay) = delay {
                let executor = cx.background_executor().clone();
                return cx.background_spawn(async move {
                    executor.timer(delay).await;
                    let mut state = state.lock();
                    if let Some(error) = state.kill_error.clone() {
                        return Err(anyhow::anyhow!(error));
                    }
                    state.sessions.remove(&session_name);
                    Ok(())
                });
            }
            let mut state = state.lock();
            if let Some(error) = state.kill_error.clone() {
                return Task::ready(Err(anyhow::anyhow!(error)));
            }
            state.sessions.remove(&session_name);
            return Task::ready(Ok(()));
        }

        if self.path_style(cx).is_windows() {
            return Task::ready(if context.is_some() {
                Err(anyhow::anyhow!("original shpool daemon is unavailable on this platform"))
            } else { Ok(()) });
        }
        if self.fs.is_fake() {
            return Task::ready(Ok(()));
        }
        let Some(quoted_name) = ShellKind::Posix.try_quote(&session_name) else {
            return Task::ready(Err(anyhow::anyhow!(
                "cannot quote shpool session name {session_name:?}"
            )));
        };
        let command = match context {
            Some(context) => {
                let script = match context.command(&format!("kill {quoted_name}")) {
                    Ok(script) => script,
                    Err(error) => return Task::ready(Err(error)),
                };
                self.exec_shpool_context_script(script, cx)
            }
            None => {
                let script = format!(
                    r#"{SHPOOL_KILL_SCRIPT_PRELUDE}
exec "$SHPOOL_BIN" -s "$SHPOOL_SOCK" kill {quoted_name}
"#
                );
                self.exec_script_in_sh(script, cx)
            }
        };
        cx.background_spawn(async move {
            let mut command = command.await?;
            // Cancel only this client; the shared daemon and SSH master must survive.
            command.kill_on_drop(true);
            let output = command.output().await?;
            if !output.status.success() {
                let stderr = String::from_utf8_lossy(&output.stderr);
                if !Self::shpool_session_not_found(&stderr, &session_name) {
                    log::warn!(
                        "failed to kill shpool session {session_name}: {}",
                        stderr.trim()
                    );
                    anyhow::bail!("killing shpool session {session_name}: {}", stderr.trim());
                }
            }
            Ok(())
        })
    }

    /// Finds shpool and its sessions on the host the project's shells run on.
    /// `shpool list` starts the daemon when none runs, so a reachable daemon is
    /// what makes shpool available, not only the binary.
    fn probe_shpool(&mut self, cx: &mut Context<Self>) -> Task<Result<Option<ShpoolProbe>>> {
        self.probe_shpool_in_context(None, true, cx)
    }

    fn probe_shpool_in_context(
        &mut self,
        context: Option<ShpoolSessionContext>,
        allow_daemon_start: bool,
        cx: &mut Context<Self>,
    ) -> Task<Result<Option<ShpoolProbe>>> {
        let probe = self.probe_shpool_unbounded(context, allow_daemon_start, cx);
        let executor = cx.background_executor().clone();
        cx.background_spawn(async move {
            let timeout = executor.timer(SHPOOL_PROBE_TIMEOUT);
            futures::pin_mut!(probe, timeout);
            match futures::future::select(probe, timeout).await {
                futures::future::Either::Left((result, _)) => result,
                futures::future::Either::Right(((), _)) => {
                    log::warn!(
                        "shpool probe timed out after {}s",
                        SHPOOL_PROBE_TIMEOUT.as_secs()
                    );
                    Err(anyhow::anyhow!(
                        "shpool probe timed out after {}s",
                        SHPOOL_PROBE_TIMEOUT.as_secs()
                    ))
                }
            }
        })
    }

    fn probe_shpool_unbounded(
        &mut self,
        context: Option<ShpoolSessionContext>,
        allow_daemon_start: bool,
        cx: &mut Context<Self>,
    ) -> Task<Result<Option<ShpoolProbe>>> {
        if let Some(context) = &context {
            if let Err(error) = context.validate() { return Task::ready(Err(error)); }
        }
        #[cfg(any(test, feature = "test-support"))]
        if let Some(fake) = &self.fake_shpool {
            if context.as_ref().is_some_and(|context| fake.state.lock().socket.to_string_lossy().as_bytes() != context.socket.as_bytes()) {
                return Task::ready(Err(anyhow::anyhow!("original shpool daemon is unavailable")));
            }
            let (result, delay) = {
                let state = fake.state.lock();
                let result = match (&state.probe_error, state.available) {
                    (Some(error), _) => Err(anyhow::anyhow!("{error}")),
                    (None, false) => Ok(None),
                    (None, true) => Ok(Some(ShpoolProbe {
                        program: context.as_ref().map(|context| context.program.clone())
                            .unwrap_or_else(|| state.program.clone()),
                        socket: state.socket.clone(),
                        sessions: state.sessions.iter().cloned().collect(),
                    })),
                };
                (result, state.probe_delay)
            };
            if let Some(delay) = delay {
                let executor = cx.background_executor().clone();
                return cx.background_spawn(async move {
                    executor.timer(delay).await;
                    result
                });
            }
            return Task::ready(result);
        }

        // A project on a fake filesystem must not reach the sessions of the
        // machine it runs on.
        if self.path_style(cx).is_windows() || self.fs.is_fake() {
            return Task::ready(Ok(None));
        }
        let command = match context {
            Some(context) => match context.probe_script(allow_daemon_start) {
                Ok(script) => self.exec_shpool_context_script(script, cx),
                Err(error) => return Task::ready(Err(error)),
            },
            None => self.exec_script_in_sh(
                if allow_daemon_start { SHPOOL_PROBE_SCRIPT } else { SHPOOL_EXISTING_PROBE_SCRIPT }
                    .to_string(), cx,
            ),
        };
        cx.background_spawn(async move {
            let mut command = command.await?;
            // Cancel only this client; the shared daemon and SSH master must survive.
            command.kill_on_drop(true);
            let output = command.output().await?;
            parse_shpool_probe_output(&output)
        })
    }

    /// Creates a local terminal even if the project is remote.
    /// In remote projects: opens in Zed's launch directory (bypasses SSH).
    /// In local projects: opens in the project directory (same as regular terminals).
    pub fn create_local_terminal(
        &mut self,
        cx: &mut Context<Self>,
    ) -> Task<Result<Entity<Terminal>>> {
        let working_directory = if self.remote_client.is_some() {
            // Remote project: don't use remote paths, let shell use Zed's cwd
            None
        } else {
            // Local project: use project directory like normal terminals
            self.active_project_directory(cx).map(|p| p.to_path_buf())
        };
        self.create_terminal_shell_internal(
            working_directory,
            true,
            None,
            PtyStartup::default(),
            cx,
        )
    }

    /// Internal method for creating terminal shells.
    fn terminal_settings_location(
        &self,
        cwd: Option<&Path>,
        cx: &App,
    ) -> Option<SettingsLocation<'static>> {
        let (worktree, _) = self.find_worktree(cwd?, cx)?;
        Some(SettingsLocation {
            worktree_id: worktree.read(cx).id(),
            path: RelPath::empty(),
        })
    }

    /// If force_local is true, creates a local terminal even if the project has a remote client.
    /// This allows "breaking out" to a local shell in remote projects.
    /// `command` replaces the configured shell.
    fn create_terminal_shell_internal(
        &mut self,
        cwd: Option<PathBuf>,
        force_local: bool,
        command: Option<ShellCommand>,
        pty_startup: PtyStartup,
        cx: &mut Context<Self>,
    ) -> Task<Result<Entity<Terminal>>> {
        let path = cwd.map(|p| Arc::from(&*p));
        let is_via_remote = !force_local && self.remote_client.is_some();

        let settings_location = self.terminal_settings_location(path.as_deref(), cx);
        let settings = TerminalSettings::get(settings_location, cx).clone();
        let detect_venv = settings.detect_venv.as_option().is_some();
        let local_path = if is_via_remote { None } else { path.clone() };

        // See create_terminal_task: scope the toolchain lookup to the
        // worktree the terminal is opened in, not the active editor's
        // worktree or other visible worktrees.
        let project_path_contexts: Vec<ProjectPath> = path
            .as_ref()
            .and_then(|p| self.find_worktree(p, cx))
            .map(|(worktree, relative_path)| ProjectPath {
                worktree_id: worktree.read(cx).id(),
                path: relative_path,
            })
            .into_iter()
            .collect();
        // An activation script is typed into the shell, while the shell that
        // `command` starts, e.g. a reattached shpool session, may already run a program.
        let toolchains = project_path_contexts
            .into_iter()
            .filter(|_| detect_venv && command.is_none())
            .map(|p| self.active_toolchain(p, LanguageName::new_static("Python"), cx))
            .collect::<Vec<_>>();
        let remote_client = if force_local {
            None
        } else {
            self.remote_client.clone()
        };
        let remote_shell_request = remote_client.as_ref().map(|remote_client| {
            remote_client
                .read(cx)
                .proto_client()
                .request(proto::GetTerminalShell {
                    project_id: proto::REMOTE_SERVER_PROJECT_ID,
                    worktree_id: settings_location
                        .map(|settings_location| settings_location.worktree_id.to_proto()),
                })
        });
        let shell = match &remote_client {
            Some(remote_client) => remote_client
                .read(cx)
                .shell()
                .unwrap_or_else(get_default_system_shell),
            None => settings.shell.program(),
        };
        let env_shell = match &remote_client {
            Some(_) => shell.clone(),
            None => get_system_shell(),
        };

        let path_style = self.path_style(cx);

        // Prepare a task for resolving the environment
        let env_task =
            self.resolve_directory_environment(&env_shell, path.clone(), remote_client.clone(), cx);

        let lang_registry = self.languages.clone();
        cx.spawn(async move |project, cx| {
            let remote_shell = if let Some(remote_shell_request) = remote_shell_request {
                let response = remote_shell_request
                    .await
                    .context("failed to get terminal shell settings from remote server")?;
                let shell = response
                    .shell
                    .context("remote server returned no terminal shell")?;
                let shell = task::shell_from_proto(shell)
                    .context("remote server returned an invalid terminal shell")?;
                log::debug!(
                    "create_terminal_shell_internal: using remote terminal shell setting: {shell:?}"
                );
                Some(shell)
            } else {
                None
            };
            let command = command
                .map(|command| -> Result<_> {
                    let configured_shell = remote_shell.as_ref().unwrap_or(&settings.shell);
                    let args = build_shpool_attach_args(
                        &command.socket,
                        path.as_deref(),
                        &command.session_name,
                        configured_shell,
                    )?;
                    let title_override = match configured_shell {
                        Shell::WithArguments { title_override: Some(title), .. } => Some(title.clone()),
                        _ => command.title_override,
                    };
                    Ok((command.program, args, title_override))
                })
                .transpose()?;
            let shell_program = remote_shell.as_ref().map(Shell::program).unwrap_or(shell);
            let shell_kind = ShellKind::new(&shell_program, path_style.is_windows());
            let mut env = env_task.await.unwrap_or_default();
            env.extend(settings.env);

            let activation_script = maybe!(async {
                for toolchain in toolchains {
                    let Some(toolchain) = toolchain.await else {
                        continue;
                    };
                    let language = lang_registry
                        .language_for_name(&toolchain.language_name.0)
                        .await
                        .ok();
                    let lister = language?.toolchain_lister()?;
                    let future =
                        cx.update(|cx| lister.activation_script(&toolchain, shell_kind, cx));
                    return Some(future.await);
                }
                None
            })
            .await
            .unwrap_or_default();

            let builder = project
                .update(cx, move |_, cx| {
                    let (shell, env) = {
                        match remote_client {
                            Some(remote_client) => {
                                let empty_args = Vec::new();
                                let spawn_command = match (&command, remote_shell.as_ref()) {
                                    (Some((program, args, _)), _) => Some((program, args)),
                                    (None, Some(Shell::System) | None) => None,
                                    (None, Some(Shell::Program(program))) => {
                                        Some((program, &empty_args))
                                    }
                                    (None, Some(Shell::WithArguments { program, args, .. })) => {
                                        Some((program, args))
                                    }
                                };
                                create_remote_shell(spawn_command, env, path, remote_client, cx)?
                            }
                            None => match command {
                                Some((program, args, title_override)) => (
                                    Shell::WithArguments {
                                        program,
                                        args,
                                        title_override,
                                    },
                                    env,
                                ),
                                None => (settings.shell, env),
                            },
                        }
                    };
                    anyhow::Ok(TerminalBuilder::new(
                        local_path.map(|path| path.to_path_buf()),
                        TerminalMode::interactive(),
                        shell,
                        env,
                        settings.cursor_shape,
                        settings.alternate_scroll,
                        settings.max_scroll_history_lines,
                        settings.path_hyperlink_regexes,
                        Duration::from_millis(settings.path_hyperlink_timeout_ms),
                        is_via_remote,
                        cx.entity_id().as_u64(),
                        cx,
                        activation_script,
                        path_style,
                        pty_startup,
                    ))
                })??
                .await?;
            project.update(cx, move |this, cx| {
                let terminal_handle = cx.new(|cx| builder.subscribe(cx));

                this.terminals
                    .local_handles
                    .push(terminal_handle.downgrade());

                let id = terminal_handle.entity_id();
                cx.observe_release(&terminal_handle, move |project, _terminal, cx| {
                    let handles = &mut project.terminals.local_handles;

                    if let Some(index) = handles
                        .iter()
                        .position(|terminal| terminal.entity_id() == id)
                    {
                        handles.remove(index);
                        cx.notify();
                    }
                })
                .detach();

                terminal_handle
            })
        })
    }

    pub fn clone_terminal(
        &mut self,
        terminal: &Entity<Terminal>,
        cx: &mut Context<'_, Project>,
        cwd: Option<PathBuf>,
    ) -> Task<Result<Entity<Terminal>>> {
        // We cannot clone the task's terminal, as it will effectively re-spawn the task, which might not be desirable.
        // For now, create a new shell instead.
        if terminal.read(cx).task().is_some() {
            return self.create_terminal_shell(cwd, cx);
        }
        let local_path = if self.is_via_remote_server() {
            None
        } else {
            cwd
        };

        let builder = terminal.read(cx).clone_builder(cx, local_path);
        cx.spawn(async |project, cx| {
            let terminal = builder.await?;
            project.update(cx, |project, cx| {
                let terminal_handle = cx.new(|cx| terminal.subscribe(cx));

                project
                    .terminals
                    .local_handles
                    .push(terminal_handle.downgrade());

                let id = terminal_handle.entity_id();
                cx.observe_release(&terminal_handle, move |project, _terminal, cx| {
                    let handles = &mut project.terminals.local_handles;

                    if let Some(index) = handles
                        .iter()
                        .position(|terminal| terminal.entity_id() == id)
                    {
                        handles.remove(index);
                        cx.notify();
                    }
                })
                .detach();

                terminal_handle
            })
        })
    }

    pub fn terminal_settings<'a>(
        &'a self,
        path: &'a Option<PathBuf>,
        cx: &'a App,
    ) -> &'a TerminalSettings {
        let mut settings_location = None;
        if let Some(path) = path.as_ref()
            && let Some((worktree, _)) = self.find_worktree(path, cx)
        {
            settings_location = Some(SettingsLocation {
                worktree_id: worktree.read(cx).id(),
                path: RelPath::empty(),
            });
        }
        TerminalSettings::get(settings_location, cx)
    }

    /// A captured daemon locator must not be reinterpreted through current
    /// project roots, shell settings, PATH, HOME or XDG_RUNTIME_DIR.
    fn exec_shpool_context_script(
        &self,
        script: String,
        cx: &mut Context<Self>,
    ) -> Task<Result<smol::process::Command>> {
        if self.path_style(cx).is_windows() {
            return Task::ready(Err(anyhow::anyhow!("POSIX sh is not supported on Windows")));
        }
        let args = vec!["-c".to_string(), script];
        let result = (|| {
            let mut command = if let Some(remote_client) = &self.remote_client {
                let template = remote_client.read(cx).build_command(
                    Some("/bin/sh".into()), &args, &HashMap::<String, String>::default(),
                    None, None, Interactive::No,
                )?;
                let mut command = new_std_command(template.program);
                command.args(template.args).envs(template.env);
                command
            } else {
                let mut command = new_std_command("/bin/sh");
                command.args(args);
                command
            };
            util::set_pre_exec_to_start_new_session(&mut command);
            anyhow::Ok(smol::process::Command::from(command))
        })();
        Task::ready(result)
    }

    pub fn exec_script_in_sh(
        &self,
        script: String,
        cx: &mut Context<Self>,
    ) -> Task<Result<smol::process::Command>> {
        if self.path_style(cx).is_windows() {
            return Task::ready(Err(anyhow::anyhow!("POSIX sh is not supported on Windows")));
        }

        let path = self.first_project_directory(cx);
        let remote_client = self.remote_client.clone();
        let settings = self.terminal_settings(&path, cx).clone();
        let program = "/bin/sh".to_string();
        let args = vec!["-c".to_string(), script];

        let env_task = self.resolve_directory_environment(
            &program,
            path.as_ref().map(|path| Arc::from(&**path)),
            remote_client.clone(),
            cx,
        );

        cx.spawn(async move |project, cx| {
            let mut env = env_task.await.unwrap_or_default();
            env.extend(settings.env);

            project.update(cx, move |_, cx| {
                match remote_client {
                    Some(remote_client) => {
                        let command_template = remote_client.read(cx).build_command(
                            Some(program),
                            &args,
                            &env,
                            None,
                            None,
                            Interactive::No,
                        )?;
                        let mut command = new_std_command(command_template.program);
                        command.args(command_template.args);
                        command.envs(command_template.env);
                        Ok(command)
                    }
                    None => {
                        let mut command = new_std_command(program);
                        command.args(args);
                        command.envs(env);
                        if let Some(path) = path {
                            command.current_dir(path);
                        }
                        Ok(command)
                    }
                }
                .map(|mut process| {
                    util::set_pre_exec_to_start_new_session(&mut process);
                    smol::process::Command::from(process)
                })
            })?
        })
    }

    pub fn exec_in_shell(
        &self,
        command: String,
        cx: &mut Context<Self>,
    ) -> Task<Result<smol::process::Command>> {
        let path = self.first_project_directory(cx);
        let remote_client = self.remote_client.clone();
        let settings = self.terminal_settings(&path, cx).clone();
        let shell = remote_client
            .as_ref()
            .and_then(|remote_client| remote_client.read(cx).shell())
            .map(Shell::Program)
            .unwrap_or(Shell::System);
        let is_windows = self.path_style(cx).is_windows();
        let builder = ShellBuilder::new(&shell, is_windows).non_interactive();
        let (command, args) = builder.build(Some(command), &Vec::new());

        let env_task = self.resolve_directory_environment(
            &shell.program(),
            path.as_ref().map(|p| Arc::from(&**p)),
            remote_client.clone(),
            cx,
        );

        cx.spawn(async move |project, cx| {
            let mut env = env_task.await.unwrap_or_default();
            env.extend(settings.env);

            project.update(cx, move |_, cx| {
                match remote_client {
                    Some(remote_client) => {
                        let command_template = remote_client.read(cx).build_command(
                            Some(command),
                            &args,
                            &env,
                            None,
                            None,
                            Interactive::Yes,
                        )?;
                        let mut command = new_std_command(command_template.program);
                        command.args(command_template.args);
                        command.envs(command_template.env);
                        Ok(command)
                    }
                    None => {
                        let mut command = new_std_command(command);
                        command.args(args);
                        command.envs(env);
                        if let Some(path) = path {
                            command.current_dir(path);
                        }
                        Ok(command)
                    }
                }
                .map(|mut process| {
                    util::set_pre_exec_to_start_new_session(&mut process);
                    smol::process::Command::from(process)
                })
            })?
        })
    }

    pub fn local_terminal_handles(&self) -> &Vec<WeakEntity<terminal::Terminal>> {
        &self.terminals.local_handles
    }

    fn resolve_directory_environment(
        &self,
        shell: &str,
        path: Option<Arc<Path>>,
        remote_client: Option<Entity<RemoteClient>>,
        cx: &mut App,
    ) -> Shared<Task<Option<HashMap<String, String>>>> {
        if let Some(path) = &path {
            let shell = Shell::Program(shell.to_string());
            self.environment
                .update(cx, |project_env, cx| match &remote_client {
                    Some(remote_client) => project_env.remote_directory_environment(
                        &shell,
                        path.clone(),
                        remote_client.clone(),
                        cx,
                    ),
                    None => project_env.local_directory_environment(&shell, path.clone(), cx),
                })
        } else {
            Task::ready(None).shared()
        }
    }
}

fn create_remote_shell(
    spawn_command: Option<(&String, &Vec<String>)>,
    mut env: HashMap<String, String>,
    working_directory: Option<Arc<Path>>,
    remote_client: Entity<RemoteClient>,
    cx: &mut App,
) -> Result<(Shell, HashMap<String, String>)> {
    insert_zed_terminal_env(&mut env, &release_channel::AppVersion::global(cx));

    let (program, args) = match spawn_command {
        Some((program, args)) => (Some(program.clone()), args),
        None => (None, &Vec::new()),
    };

    let command = remote_client.read(cx).build_command(
        program,
        args.as_slice(),
        &env,
        working_directory.map(|path| path.display().to_string()),
        None,
        Interactive::Yes,
    )?;

    log::debug!("Connecting to a remote server: {:?}", command.program);
    let host = remote_client.read(cx).connection_options().display_name();

    Ok((
        Shell::WithArguments {
            program: command.program,
            args: command.args,
            title_override: Some(format!("{} — Terminal", host)),
        },
        command.env,
    ))
}

fn format_task_for_activation(
    spawn_task: &SpawnInTerminal,
    shell_kind: ShellKind,
    shell: &str,
    is_windows: bool,
) -> String {
    if let Some(command) = &spawn_task.command {
        let command = shell_kind.prepend_command_prefix(command);
        let command = shell_kind.try_quote_prefix_aware(&command);
        let args = spawn_task
            .args
            .iter()
            .enumerate()
            .filter_map(|(index, arg)| {
                quote_prepared_task_arg_for_activation(
                    spawn_task, shell_kind, arg, index, is_windows,
                )
            });

        command.into_iter().chain(args).join(" ")
    } else {
        // todo: this breaks for remotes to windows
        format!("exec {shell} -l")
    }
}

fn quote_prepared_task_arg_for_activation<'a>(
    spawn_task: &SpawnInTerminal,
    shell_kind: ShellKind,
    arg: &'a str,
    index: usize,
    is_windows: bool,
) -> Option<Cow<'a, str>> {
    if spawn_task.shell.shell_kind(is_windows) == ShellKind::Cmd
        && index >= 2
        && spawn_task
            .args
            .get(index - 2)
            .is_some_and(|arg| arg.eq_ignore_ascii_case("/S"))
        && spawn_task
            .args
            .get(index - 1)
            .is_some_and(|arg| arg.eq_ignore_ascii_case("/C"))
    {
        // The /C argument is already a cmd command string from prepare_task_for_spawn.
        // Quoting it again for venv activation makes cmd see the quotes as literals.
        return quote_cmd_command_arg_for_outer_shell(arg, shell_kind).map(Cow::Owned);
    }

    shell_kind.try_quote(arg)
}

fn quote_cmd_command_arg_for_outer_shell(arg: &str, shell_kind: ShellKind) -> Option<String> {
    match shell_kind {
        ShellKind::PowerShell | ShellKind::Pwsh => Some(format!("'{}'", arg.replace('\'', "''"))),
        ShellKind::Cmd => Some(arg.to_string()),
        ShellKind::Posix
        | ShellKind::Csh
        | ShellKind::Tcsh
        | ShellKind::Fish
        | ShellKind::Nushell
        | ShellKind::Rc
        | ShellKind::Xonsh
        | ShellKind::Elvish => shell_kind.try_quote(arg).map(Cow::into_owned),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use pretty_assertions::assert_eq;

    fn prepared_cmd_task(command_arg: &str) -> SpawnInTerminal {
        SpawnInTerminal {
            command: Some("cmd.exe".to_string()),
            args: vec!["/S".to_string(), "/C".to_string(), command_arg.to_string()],
            shell: Shell::Program("cmd.exe".to_string()),
            ..SpawnInTerminal::default()
        }
    }

    #[test]
    fn formats_prepared_cmd_task_for_powershell_activation() {
        let task = prepared_cmd_task("\"echo Hi there\"");

        assert_eq!(
            format_task_for_activation(&task, ShellKind::PowerShell, "powershell.exe", true),
            "&cmd.exe /S /C '\"echo Hi there\"'"
        );
    }

    #[test]
    fn formats_prepared_cmd_task_for_cmd_activation() {
        let task = prepared_cmd_task("\"echo Hi there\"");

        assert_eq!(
            format_task_for_activation(&task, ShellKind::Cmd, "cmd.exe", true),
            "cmd.exe /S /C \"echo Hi there\""
        );
    }

    #[test]
    fn formats_prepared_cmd_task_with_shell_args_for_activation() {
        let task = SpawnInTerminal {
            command: Some("cmd.exe".to_string()),
            args: vec![
                "/D".to_string(),
                "/S".to_string(),
                "/C".to_string(),
                "\"echo Hi there\"".to_string(),
            ],
            shell: Shell::WithArguments {
                program: "cmd.exe".to_string(),
                args: vec!["/D".to_string()],
                title_override: None,
            },
            ..SpawnInTerminal::default()
        };

        assert_eq!(
            format_task_for_activation(&task, ShellKind::PowerShell, "powershell.exe", true),
            "&cmd.exe /D /S /C '\"echo Hi there\"'"
        );
    }

    #[test]
    fn formats_prepared_cmd_task_with_single_quote_for_powershell_activation() {
        let task = prepared_cmd_task("\"echo It's fine\"");

        assert_eq!(
            format_task_for_activation(&task, ShellKind::PowerShell, "powershell.exe", true),
            "&cmd.exe /S /C '\"echo It''s fine\"'"
        );
    }

    #[test]
    fn formats_non_cmd_task_for_activation() {
        let task = SpawnInTerminal {
            command: Some("cargo".to_string()),
            args: vec!["test".to_string(), "some test".to_string()],
            shell: Shell::System,
            ..SpawnInTerminal::default()
        };

        assert_eq!(
            format_task_for_activation(&task, ShellKind::PowerShell, "powershell.exe", true),
            "&cargo test 'some test'"
        );
    }

    #[gpui::test]
    async fn test_fake_shpool_seam_lifecycle(cx: &mut gpui::TestAppContext) {
        cx.update(|cx| {
            let store = settings::SettingsStore::test(cx);
            cx.set_global(store);
            terminal::terminal_settings::TerminalSettings::register(cx);
        });
        let fs = fs::FakeFs::new(cx.executor());
        let project = Project::test(fs, [], cx).await;

        let probe = project
            .update(cx, |p, cx| p.probe_shpool(cx))
            .await
            .expect("probe should succeed");
        assert!(probe.is_none());

        project.update(cx, |p, _| {
            p.enable_fake_shpool(&["zed-existing"]);
        });

        let probe = project
            .update(cx, |p, cx| p.probe_shpool(cx))
            .await
            .expect("probe should succeed")
            .expect("fake shpool should be available");
        assert_eq!(probe.program, "shpool");
        assert_eq!(probe.sessions, vec!["zed-existing"]);

        let term1 = project
            .update(cx, |p, cx| {
                p.create_persistent_terminal_shell(None, "zed-existing".to_string(), None, cx)
            })
            .await
            .expect("should spawn persistent terminal");
        assert_eq!(term1.session, PersistentSession::Reattached);
        assert_eq!(
            project.read_with(cx, |p, _| p.fake_shpool_attached_sessions()),
            vec!["zed-existing"]
        );

        let term2 = project
            .update(cx, |p, cx| {
                p.create_persistent_terminal_shell(None, "zed-new".to_string(), None, cx)
            })
            .await
            .expect("should spawn persistent terminal");
        assert_eq!(term2.session, PersistentSession::Created);
        assert_eq!(
            project.read_with(cx, |p, _| p.fake_shpool_attached_sessions()),
            vec!["zed-existing", "zed-new"]
        );
        let attach_args = project.read_with(cx, |p, _| p.fake_shpool_attached_args());
        assert_eq!(attach_args.len(), 2);
        let (existing_session, existing_args) = &attach_args[0];
        assert_eq!(existing_session, "zed-existing");
        assert!(
            existing_args.contains(&"--cmd".to_string()),
            "reattached session must also pass --cmd for dead-session respawn"
        );
        let existing_cmd_index = existing_args
            .iter()
            .position(|argument| argument == "--cmd")
            .expect("missing --cmd on existing session attach");
        assert_eq!(
            existing_args[existing_cmd_index + 1],
            "/bin/sh -c 'exec \"$SHELL\" -l'"
        );
        let (new_session, new_args) = &attach_args[1];
        assert_eq!(new_session, "zed-new");
        assert!(
            new_args.contains(&"--cmd".to_string()),
            "new session must pass --cmd to bypass shpool prompt sentinels"
        );
        let new_cmd_index = new_args
            .iter()
            .position(|argument| argument == "--cmd")
            .expect("missing --cmd on new session attach");
        assert_eq!(new_args[new_cmd_index + 1], "/bin/sh -c 'exec \"$SHELL\" -l'");

        project
            .update(cx, |p, cx| {
                p.kill_persistent_terminal_session("zed-existing".to_string(), cx)
            })
            .await
            .expect("should kill session");
        assert_eq!(
            project.read_with(cx, |p, _| p.fake_shpool_killed_sessions()),
            vec!["zed-existing"]
        );
        let fake = project
            .read_with(cx, |p, _| p.fake_shpool())
            .expect("fake shpool exists");
        assert!(!fake.has_session("zed-existing"));
        assert!(fake.has_session("zed-new"));
    }

    #[gpui::test]
    async fn test_persistent_terminal_keeps_original_daemon_after_root_and_client_changes(
        cx: &mut gpui::TestAppContext,
    ) {
        cx.update(|cx| {
            let store = settings::SettingsStore::test(cx);
            cx.set_global(store);
            TerminalSettings::register(cx);
        });
        let fs = fs::FakeFs::new(cx.executor());
        fs.insert_tree("/first", serde_json::json!({})).await;
        fs.insert_tree("/second", serde_json::json!({})).await;
        let project = Project::test(fs, [Path::new("/first"), Path::new("/second")], cx).await;
        let fake = project.update(cx, |project, _| project.enable_fake_shpool(&[]));
        let original = project.update(cx, |project, cx| {
            project.create_persistent_terminal_shell(None, "zed-context".into(), None, cx)
        }).await.unwrap();
        assert_eq!(original.session, PersistentSession::Created);
        let context = project.read_with(cx, |project, _| {
            project.persistent_terminal_context("zed-context").cloned().unwrap()
        });
        assert_eq!(context, ShpoolSessionContext::for_test());
        project.update(cx, |project, cx| {
            let first = project.worktrees(cx).next().unwrap().read(cx).id();
            project.remove_worktree(first, cx);
        });
        fake.state.lock().program = "/different/shpool".into();
        let restored = project.update(cx, |project, cx| {
            project.create_persistent_terminal_shell_in_context(
                None, "zed-context".into(), None, Some(context.clone()), true, cx,
            )
        }).await.unwrap();
        assert_eq!(restored.session, PersistentSession::Reattached);
        assert_eq!(project.read_with(cx, |project, _| {
            project.persistent_terminal_context("zed-context").cloned()
        }), Some(context.clone()));
        project.update(cx, |project, cx| {
            project.kill_persistent_terminal_session("zed-context".into(), cx)
        }).await.unwrap();
        assert!(!fake.has_session("zed-context"));
        assert_eq!(fake.kill_history(), vec!["zed-context".to_string()]);
        let command = context.command("kill zed-context").unwrap();
        assert!(command.contains("/shpool"));
        assert!(!command.contains("/different/shpool"));
        assert!(command.contains(" -D -s "));
    }

    #[gpui::test]
    async fn test_unknown_legacy_daemon_requires_a_positive_session_match(
        cx: &mut gpui::TestAppContext,
    ) {
        cx.update(|cx| {
            let store = settings::SettingsStore::test(cx);
            cx.set_global(store);
            TerminalSettings::register(cx);
        });
        let fs = fs::FakeFs::new(cx.executor());
        let project = Project::test(fs, [], cx).await;
        let fake = project.update(cx, |project, _| project.enable_fake_shpool(&[]));
        assert!(project.update(cx, |project, cx| {
            project.kill_persistent_terminal_session_in_context("zed-legacy".into(), None, cx)
        }).await.is_err());
        assert!(fake.kill_history().is_empty());
        assert!(project.update(cx, |project, cx| {
            project.create_persistent_terminal_shell_in_context(
                None, "zed-legacy".into(), None, None, true, cx,
            )
        }).await.is_err());
        assert!(fake.attach_history().is_empty(), "unknown legacy sessions must not be duplicated");
        fake.add_session("zed-legacy");
        let restored = project.update(cx, |project, cx| {
            project.create_persistent_terminal_shell_in_context(
                None, "zed-legacy".into(), None, None, true, cx,
            )
        }).await.unwrap();
        assert_eq!(restored.session, PersistentSession::Reattached);
        assert_eq!(project.read_with(cx, |project, _| {
            project.persistent_terminal_context("zed-legacy").cloned()
        }), Some(ShpoolSessionContext::for_test()));
    }

    #[gpui::test]
    async fn test_unavailable_pinned_daemon_never_falls_back_to_another_socket(
        cx: &mut gpui::TestAppContext,
    ) {
        cx.update(|cx| {
            let store = settings::SettingsStore::test(cx);
            cx.set_global(store);
            TerminalSettings::register(cx);
        });
        let fs = fs::FakeFs::new(cx.executor());
        let project = Project::test(fs, [], cx).await;
        let fake = project.update(cx, |project, _| project.enable_fake_shpool(&[]));
        let _terminal = project.update(cx, |project, cx| {
            project.create_persistent_terminal_shell(None, "zed-original".into(), None, cx)
        }).await.unwrap();
        let context = project.read_with(cx, |project, _| {
            project.persistent_terminal_context("zed-original").cloned().unwrap()
        });
        fake.state.lock().socket = "/tmp/other-daemon.socket".into();
        fake.add_session("unrelated");
        assert!(project.update(cx, |project, cx| {
            project.kill_persistent_terminal_session_in_context(
                "zed-original".into(), Some(context.clone()), cx,
            )
        }).await.is_err());
        assert!(project.update(cx, |project, cx| {
            project.create_persistent_terminal_shell_in_context(
                None, "zed-original".into(), None, Some(context.clone()), true, cx,
            )
        }).await.is_err());
        assert!(fake.kill_history().is_empty());
        assert_eq!(fake.attach_history(), vec!["zed-original".to_string()]);
        assert!(fake.has_session("zed-original"));
        assert!(fake.has_session("unrelated"));
        assert_eq!(project.read_with(cx, |project, _| {
            project.persistent_terminal_context("zed-original").cloned()
        }), Some(context));
    }

    #[test]
    fn test_shpool_command_preserves_shell_program_and_arguments() {
        assert_eq!(
            shpool_shell_command(&Shell::System, None).unwrap(),
            "/bin/sh -c 'exec \"$SHELL\" -l'"
        );
        assert_eq!(
            shpool_shell_command(&Shell::Program("nu".into()), None).unwrap(),
            "nu"
        );
        let shell = Shell::WithArguments {
            program: "zsh".into(),
            args: vec!["--no-rcs".into(), "-i".into()],
            title_override: None,
        };
        assert_eq!(
            shpool_shell_command(&shell, None).unwrap(),
            "zsh --no-rcs -i"
        );
        assert!(
            shpool_shell_command(&Shell::Program("invalid\0shell".into()), None).is_err()
        );
    }

    #[test]
    #[cfg(unix)]
    fn test_shpool_command_round_trips_literal_shell_arguments() {
        for values in [
            vec!["plain", "$HOME", "", "quote'\";$(false)", "trailing\n\n"],
            vec!["literal {workspace}", "$HOME", "", "Юникод", "trailing\n\n"],
        ] {
            let mut args = vec![
                "-c".into(),
                r#"printf '%s\0' "$@""#.into(),
                "configured-shell".into(),
            ];
            args.extend(values.iter().map(|value| value.to_string()));
            let shell = Shell::WithArguments {
                program: "/bin/sh".into(),
                args,
                title_override: None,
            };
            let command = shpool_shell_command(&shell, None).unwrap();
            assert!(!command.contains('{') && !command.contains('}'));
            let output = std::process::Command::new("/bin/sh")
                .args(["-c", &command])
                .output()
                .expect("execute the configured shell command");
            assert!(output.status.success(), "{:?}", output);
            let expected: Vec<u8> = values
                .iter()
                .flat_map(|value| value.as_bytes().iter().copied().chain([0]))
                .collect();
            assert_eq!(output.stdout, expected);
        }
    }

    #[gpui::test]
    async fn test_persistent_terminal_honors_configured_shell_arguments(
        cx: &mut gpui::TestAppContext,
    ) {
        cx.update(|cx| {
            let store = settings::SettingsStore::test(cx);
            cx.set_global(store);
            TerminalSettings::register(cx);
            let mut settings = TerminalSettings::get(None, cx).clone();
            settings.shell = Shell::WithArguments {
                program: "zsh".into(),
                args: vec!["--no-rcs".into(), "-i".into()],
                title_override: None,
            };
            TerminalSettings::override_global(settings, cx);
        });
        let fs = fs::FakeFs::new(cx.executor());
        let project = Project::test(fs, [], cx).await;
        project.update(cx, |project, _| {
            project.enable_fake_shpool(&[]);
        });
        project
            .update(cx, |project, cx| {
                project.create_persistent_terminal_shell(
                    None, "configured-shell".into(), None, cx,
                )
            })
            .await
            .expect("create the configured persistent terminal");
        let attached = project.read_with(cx, |project, _| project.fake_shpool_attached_args());
        assert_eq!(attached.len(), 1);
        let args = &attached[0].1;
        let command = args.iter().position(|arg| arg == "--cmd").unwrap();
        assert_eq!(args[command + 1], "zsh --no-rcs -i");
    }

    #[test]
    fn test_build_shpool_attach_args() {
        let socket = Path::new("/run/user/1000/shpool/shpool.socket");
        let cwd = Path::new("/home/user/project");

        let attach_args_with_cwd = build_shpool_attach_args(socket, Some(cwd), "zed-thread-1", &Shell::System).unwrap();
        assert_eq!(
            attach_args_with_cwd,
            vec![
                "-s",
                "/run/user/1000/shpool/shpool.socket",
                "attach",
                "--force",
                "--dir",
                "/home/user/project",
                "--cmd",
                "/bin/sh -c 'exec \"$SHELL\" -l'",
                "zed-thread-1",
            ]
        );

        let attach_args_without_cwd = build_shpool_attach_args(socket, None, "zed-thread-1", &Shell::System).unwrap();
        assert_eq!(
            attach_args_without_cwd,
            vec![
                "-s",
                "/run/user/1000/shpool/shpool.socket",
                "attach",
                "--force",
                "--cmd",
                "/bin/sh -c 'exec \"$SHELL\" -l'",
                "zed-thread-1",
            ]
        );
    }

    #[test]
    fn test_parse_shpool_probe_multiline_socket() {
        let stdout = format!(
            "{SHPOOL_PROBE_MARKER}\n/home/core/.local/bin/shpool\n/run/user/1000/shpool/shpool.socket\n{{\"sessions\":[{{\"name\":\"zed-session-1\"}}]}}"
        );
        let probe = parse_shpool_probe(&stdout)
            .expect("should parse 3-line probe output")
            .expect("marker found");
        assert_eq!(probe.program, "/home/core/.local/bin/shpool");
        assert_eq!(
            probe.socket,
            PathBuf::from("/run/user/1000/shpool/shpool.socket")
        );
        assert_eq!(probe.sessions, vec!["zed-session-1"]);

        // Absent shpool (no marker) returns Ok(None)
        assert!(parse_shpool_probe("").unwrap().is_none());
        assert!(parse_shpool_probe("   \n   \n").unwrap().is_none());

        // 2-line output is rejected (no fallback to relative socket)
        let stdout_2line = format!(
            "{SHPOOL_PROBE_MARKER}\nshpool\n{{\"sessions\":[{{\"name\":\"zed-session-2\"}}]}}"
        );
        assert!(
            parse_shpool_probe(&stdout_2line).is_err(),
            "2-line output without socket path must return error"
        );

        // Relative socket path is rejected
        let stdout_relative = format!(
            "{SHPOOL_PROBE_MARKER}\n/home/core/.local/bin/shpool\nshpool.socket\n{{\"sessions\":[{{\"name\":\"zed-session-1\"}}]}}"
        );
        assert!(
            parse_shpool_probe(&stdout_relative).is_err(),
            "relative socket path must return error"
        );

        // Invalid JSON is rejected
        let stdout_invalid_json =
            format!("{SHPOOL_PROBE_MARKER}\n/bin/shpool\n/run/socket\nnot json");
        assert!(
            parse_shpool_probe(&stdout_invalid_json).is_err(),
            "invalid JSON must return error"
        );
    }

    #[test]
    fn test_parse_shpool_probe_robust_against_rc_banners() {
        let stdout = r#"
Welcome to Ubuntu 24.04.1 LTS (GNU/Linux 6.8.0-40-generic x86_64)
Last login: Wed Oct 14 20:00:00 2026 from 10.0.0.1
[user@server ~]$ echo banner noise
=== ZED_SHPOOL_PROBE_START ===
/usr/bin/shpool
/run/user/1000/shpool/shpool.socket
{"sessions":[{"name":"zed-active-1"},{"name":"zed-active-2"}]}
"#;
        let probe = parse_shpool_probe(stdout)
            .expect("parse must succeed")
            .expect("marker must be found");
        assert_eq!(probe.program, "/usr/bin/shpool");
        assert_eq!(
            probe.socket,
            PathBuf::from("/run/user/1000/shpool/shpool.socket")
        );
        assert_eq!(
            probe.sessions,
            vec!["zed-active-1".to_string(), "zed-active-2".to_string()]
        );
    }

    #[gpui::test]
    async fn test_create_persistent_terminal_fails_when_probe_fails(cx: &mut gpui::TestAppContext) {
        cx.executor().allow_parking();
        cx.update(|cx| {
            let store = settings::SettingsStore::test(cx);
            cx.set_global(store);
            terminal::terminal_settings::TerminalSettings::register(cx);
        });
        let fs = fs::FakeFs::new(cx.executor());
        let project = Project::test(fs, [], cx).await;

        let fake = FakeShpoolStore::new();
        fake.state.lock().probe_error = Some("shpool daemon socket connection refused".to_string());
        project.update(cx, |p, _| p.set_fake_shpool(fake));

        let result = project
            .update(cx, |p, cx| {
                p.create_persistent_terminal_shell(None, "zed-probe-fail".to_string(), None, cx)
            })
            .await;

        assert!(
            result.is_err(),
            "probe failure must NOT spawn a fallback plain shell"
        );
        let err_msg = result
            .err()
            .expect("probe failure must NOT spawn a fallback plain shell")
            .to_string();
        assert!(
            err_msg.contains("shpool daemon socket connection refused"),
            "expected daemon connection refused error, got: {err_msg}"
        );
    }

    #[gpui::test]
    async fn test_shpool_kill_timeout_is_retryable_and_cancels_delayed_kill(cx: &mut gpui::TestAppContext) {
        cx.executor().allow_parking();
        cx.update(|cx| {
            let store = settings::SettingsStore::test(cx);
            cx.set_global(store);
            TerminalSettings::register(cx);
        });
        let project = Project::test(fs::FakeFs::new(cx.executor()), [], cx).await;
        let fake = FakeShpoolStore::new();
        fake.add_session("zed-timeout-kill");
        fake.state.lock().kill_delay = Some(Duration::from_secs(60));
        project.update(cx, |project, _| project.set_fake_shpool(fake.clone()));
        let task = project.update(cx, |project, cx| {
            project.kill_persistent_terminal_session("zed-timeout-kill".into(), cx)
        });
        cx.run_until_parked();
        cx.executor().advance_clock(SHPOOL_KILL_TIMEOUT + Duration::from_secs(1));
        cx.run_until_parked();
        assert!(task.await.unwrap_err().to_string().contains("kill timed out after 17s"));
        assert!(fake.has_session("zed-timeout-kill"));
        cx.executor().advance_clock(Duration::from_secs(60));
        cx.run_until_parked();
        assert!(fake.has_session("zed-timeout-kill"), "cancelled kill must not complete late");
        fake.state.lock().kill_delay = None;
        project.update(cx, |project, cx| {
            project.kill_persistent_terminal_session("zed-timeout-kill".into(), cx)
        }).await.unwrap();
        assert_eq!(fake.kill_history().len(), 2);
        assert!(!fake.has_session("zed-timeout-kill"));
    }

    #[gpui::test]
    async fn test_shpool_probe_timeout_returns_error_without_plain_shell_fallback(
        cx: &mut gpui::TestAppContext,
    ) {
        cx.executor().allow_parking();
        cx.update(|cx| {
            let store = settings::SettingsStore::test(cx);
            cx.set_global(store);
            terminal::terminal_settings::TerminalSettings::register(cx);
        });
        let fs = fs::FakeFs::new(cx.executor());
        let project = Project::test(fs, [], cx).await;

        let fake = FakeShpoolStore::new();
        fake.state.lock().probe_delay = Some(SHPOOL_PROBE_TIMEOUT + Duration::from_secs(1));
        project.update(cx, |project, _| project.set_fake_shpool(fake.clone()));

        let terminal_task = project.update(cx, |project, cx| {
            project.create_persistent_terminal_shell(
                None,
                "zed-timed-out-probe".to_string(),
                None,
                cx,
            )
        });
        cx.run_until_parked();
        cx.executor()
            .advance_clock(SHPOOL_PROBE_TIMEOUT + Duration::from_secs(2));
        cx.run_until_parked();

        let error = terminal_task
            .await
            .err()
            .expect("timed-out probe must not create a plain shell");
        assert!(
            error
                .to_string()
                .contains("shpool probe timed out after 17s"),
            "unexpected timeout error: {error:#}"
        );
        assert!(
            fake.attach_history().is_empty(),
            "timed-out probe must not attach a plain shell or shpool session"
        );
    }

    #[test]
    #[cfg(unix)]
    fn test_shpool_probe_output_nonzero_exit_bails() {
        use std::os::unix::process::ExitStatusExt;
        let valid_stdout = format!(
            "{SHPOOL_PROBE_MARKER}\n/usr/bin/shpool\n/run/user/1000/shpool/shpool.socket\n{{\"sessions\":[{{\"name\":\"zed-active-1\"}}]}}\n"
        );
        let output = std::process::Output {
            status: std::process::ExitStatus::from_raw(1 << 8), // exit code 1
            stdout: valid_stdout.into_bytes(),
            stderr: b"error: connection to shpool daemon failed: connection refused\n".to_vec(),
        };
        let result = parse_shpool_probe_output(&output);
        assert!(
            result.is_err(),
            "non-zero exit status must bail even if stdout has valid probe data"
        );
        let err = result
            .err()
            .expect("non-zero exit status must bail")
            .to_string();
        assert!(
            err.contains("shpool probe script failed with status"),
            "error must describe non-zero status: {err}"
        );
        assert!(
            err.contains("connection refused"),
            "error must include stderr message: {err}"
        );
    }

    #[gpui::test]
    async fn test_create_persistent_terminal_when_shpool_absent_spawns_plain_shell(
        cx: &mut gpui::TestAppContext,
    ) {
        cx.executor().allow_parking();
        cx.update(|cx| {
            let store = settings::SettingsStore::test(cx);
            cx.set_global(store);
            terminal::terminal_settings::TerminalSettings::register(cx);
        });
        let fs = fs::FakeFs::new(cx.executor());
        let project = Project::test(fs, [], cx).await;

        let fake = FakeShpoolStore::new();
        fake.state.lock().available = false;
        project.update(cx, |p, _| p.set_fake_shpool(fake));

        let persistent = project
            .update(cx, |p, cx| {
                p.create_persistent_terminal_shell(None, "zed-plain".to_string(), None, cx)
            })
            .await
            .expect("spawning plain shell fallback should succeed");

        assert_eq!(
            persistent.session,
            PersistentSession::Unavailable,
            "absent shpool must yield PersistentSession::Unavailable"
        );
        let fake = project
            .read_with(cx, |p, _| p.fake_shpool())
            .expect("fake_shpool should be initialized on project");
        assert!(
            fake.attach_history().is_empty(),
            "absent shpool must not record any attach in history"
        );
    }

    #[gpui::test]
    #[cfg(unix)]
    async fn test_exec_script_in_sh_runs_under_posix_sh(cx: &mut gpui::TestAppContext) {
        cx.executor().allow_parking();
        cx.update(|cx| {
            let store = settings::SettingsStore::test(cx);
            cx.set_global(store);
            terminal::terminal_settings::TerminalSettings::register(cx);
        });
        let fs = fs::FakeFs::new(cx.executor());
        let project = Project::test(fs, [], cx).await;

        let task = project.update(cx, |p, cx| {
            p.exec_script_in_sh(
                "for x in item1 item2; do echo \"posix_$x\"; done".to_string(),
                cx,
            )
        });
        let mut cmd = task.await.expect("command creation should succeed");
        let output = cmd.output().await.expect("execution should succeed");
        assert!(output.status.success());
        let stdout = String::from_utf8_lossy(&output.stdout);
        assert!(stdout.contains("posix_item1"));
        assert!(stdout.contains("posix_item2"));
    }

    #[test]
    fn test_shpool_kill_ignores_only_requested_session_not_found() {
        assert!(Project::shpool_session_not_found(
            "not found: zed-session\n",
            "zed-session"
        ));
        assert!(Project::shpool_session_not_found(
            "Error: no such session: zed-session\n",
            "zed-session"
        ));
        assert!(!Project::shpool_session_not_found(
            "sh: shpool: not found\n",
            "zed-session"
        ));
        assert!(!Project::shpool_session_not_found(
            "not found: another-session\n",
            "zed-session"
        ));
    }

    #[test]
    fn test_shpool_kill_prelude_fails_when_client_is_missing() {
        assert!(SHPOOL_SCRIPT_PRELUDE.contains("[ -n \"$SHPOOL_BIN\" ] || exit 0"));
        assert!(SHPOOL_KILL_SCRIPT_PRELUDE.contains("[ -n \"$SHPOOL_BIN\" ] || exit 127"));
    }

    #[gpui::test]
    #[cfg(unix)]
    async fn test_shpool_probe_script_syntax_under_posix_sh(cx: &mut gpui::TestAppContext) {
        cx.executor().allow_parking();
        let output = smol::process::Command::new("/bin/sh")
            .args(["-n", "-c", SHPOOL_PROBE_SCRIPT])
            .output()
            .await
            .expect("sh -n execution should succeed");
        assert!(
            output.status.success(),
            "probe script syntax invalid under /bin/sh: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }

    #[cfg(unix)]
    struct IsolatedDaemonGuard {
        runtime_dir: PathBuf,
    }

    #[cfg(unix)]
    impl Drop for IsolatedDaemonGuard {
        fn drop(&mut self) {
            let pid_path = self
                .runtime_dir
                .join("shpool")
                .join("daemonized-shpool.pid");
            if let Ok(pid_str) = std::fs::read_to_string(&pid_path) {
                if let Ok(daemon_pid) = pid_str.trim().parse::<u32>() {
                    let runtime_str = self.runtime_dir.to_string_lossy().into_owned();
                    smol::block_on(async {
                        let ps_output = smol::process::Command::new("/bin/ps")
                            .args(["-p", &daemon_pid.to_string(), "-o", "command="])
                            .output()
                            .await
                            .map(|o| String::from_utf8_lossy(&o.stdout).into_owned())
                            .unwrap_or_default();
                        if ps_output.contains(&runtime_str) {
                            if let Ok(status) = smol::process::Command::new("/bin/kill")
                                .args(["-TERM", &daemon_pid.to_string()])
                                .status()
                                .await
                            {
                                if !status.success() {
                                    // already exited
                                }
                            }
                            for _ in 0..50 {
                                let check = smol::process::Command::new("/bin/kill")
                                    .args(["-0", &daemon_pid.to_string()])
                                    .status()
                                    .await;
                                if let Ok(check) = check {
                                    if !check.success() {
                                        return;
                                    }
                                }
                                std::thread::sleep(std::time::Duration::from_millis(100));
                            }
                            if let Ok(status) = smol::process::Command::new("/bin/kill")
                                .args(["-KILL", &daemon_pid.to_string()])
                                .status()
                                .await
                            {
                                if !status.success() {
                                    // already exited
                                }
                            }
                        }
                    });
                }
            }
        }
    }

    #[gpui::test]
    #[cfg(unix)]
    async fn test_isolated_shpool_daemon_probe_and_kill(cx: &mut gpui::TestAppContext) {
        // Real processes need wall time. With parking enabled, executor timers
        // advance by elapsed real time instead of simulating daemon progress.
        cx.executor().allow_parking();
        if std::env::var("ZED_TEST_REAL_SHPOOL").is_err() {
            assert!(
                std::env::var_os("CI").is_none(),
                "CI must enable the preserved real-shpool regression"
            );
            return;
        }
        let has_shpool = smol::process::Command::new("/bin/sh")
            .args(["-c", "command -v shpool || [ -x /opt/homebrew/bin/shpool ] || [ -x $HOME/.local/bin/shpool ] || [ -x $HOME/.cargo/bin/shpool ]"])
            .output()
            .await
            .map(|o| o.status.success())
            .unwrap_or(false);
        assert!(
            has_shpool,
            "the enabled real-shpool regression requires shpool on PATH"
        );

        // Darwin's default TMPDIR is deeply nested; shpool binds both control
        // and event Unix sockets. Keep this fixture's paths below sun_path's
        // limit even after /tmp is resolved to /private/tmp.
        let temp_dir = tempfile::Builder::new()
            .prefix("zed-shpool-")
            .tempdir_in("/tmp")
            .expect("short isolated tempdir creation");
        let isolated_runtime = temp_dir.path().join("runtime");
        let isolated_home = temp_dir.path().join("home");
        std::fs::create_dir_all(&isolated_runtime).expect("create isolated runtime");
        std::fs::create_dir_all(&isolated_home).expect("create isolated home");

        let _daemon_guard = IsolatedDaemonGuard {
            runtime_dir: isolated_runtime.clone(),
        };

        let runtime_str = isolated_runtime.to_string_lossy().into_owned();
        let home_str = isolated_home.to_string_lossy().into_owned();

        cx.update(|cx| {
            let mut store = settings::SettingsStore::test(cx);
            let config = serde_json::json!({
                "terminal": {
                    "env": {
                        "HOME": home_str.clone(),
                        "XDG_RUNTIME_DIR": runtime_str.clone(),
                    }
                }
            });
            store
                .set_user_settings(&config.to_string(), cx)
                .expect("user settings should apply");
            cx.set_global(store);
            terminal::terminal_settings::TerminalSettings::register(cx);
        });

        // Use RealFs with temp_dir so fs.is_fake() is false and production kill is exercised
        let fs = fs::RealFs::new(None, cx.executor());
        let project = Project::test(fs, [temp_dir.path()], cx).await;

        // 1. Run probe script: starts the isolated daemon
        let probe_script = format!(
            "export HOME={home_str:?} XDG_RUNTIME_DIR={runtime_str:?}\n{SHPOOL_PROBE_SCRIPT}"
        );
        let task = project.update(cx, |p, cx| p.exec_script_in_sh(probe_script, cx));
        let mut cmd = task.await.expect("probe command creation");
        let output = cmd.output().await.expect("probe execution");
        assert!(
            output.status.success(),
            "probe script must succeed: status={}; stdout={}; stderr={}; daemon log={}",
            output.status,
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr),
            std::fs::read_to_string(isolated_runtime.join("shpool/daemonized-shpool.log"))
                .unwrap_or_else(|error| format!("unavailable: {error}")),
        );
        let stdout = String::from_utf8_lossy(&output.stdout);
        let probe = parse_shpool_probe(&stdout)
            .expect("should parse output")
            .expect("should find probe marker");
        assert!(probe.socket.is_absolute());
        assert!(probe.socket.starts_with(&isolated_runtime));

        // 2a. Exercise the same configured command used by persistent terminals,
        // including literal template characters in its arguments and directory.
        let literal_directory = temp_dir.path().join("project-{literal}");
        std::fs::create_dir(&literal_directory).expect("create literal project directory");
        let literal_directory = std::fs::canonicalize(literal_directory).unwrap();
        let marker = temp_dir.path().join("configured-shell-output");
        let literal_argument = "literal {workspace} $HOME 'quoted'";
        let configured_shell = Shell::WithArguments {
            program: "/bin/sh".into(),
            args: vec![
                "-c".into(),
                r#"printf '%s\n' "$PWD" "$1" > "$2"; exec /bin/sh -i"#.into(),
                "configured-shell".into(),
                literal_argument.into(),
                marker.to_string_lossy().into_owned(),
            ],
            title_override: None,
        };
        let mut attach_args = build_shpool_attach_args(
            &probe.socket,
            Some(&literal_directory),
            "zed-test-isolated",
            &configured_shell,
        ).expect("build configured shpool attach arguments");
        attach_args.insert(4, "-b".into());
        let arguments = attach_args.iter()
            .map(|argument| ShellKind::Posix.try_quote(argument).unwrap())
            .collect::<Vec<_>>()
            .join(" ");
        let attach_script = format!(
            "export HOME={home_str:?} XDG_RUNTIME_DIR={runtime_str:?}\n{SHPOOL_SCRIPT_PRELUDE}\n\"$SHPOOL_BIN\" {arguments}\n"
        );
        let task = project.update(cx, |p, cx| p.exec_script_in_sh(attach_script, cx));
        let mut cmd = task.await.expect("attach command creation");
        let output = cmd.output().await.expect("attach execution");
        assert!(
            output.status.success(),
            "shpool attach -b must succeed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        let expected_output = format!("{}\n{literal_argument}\n", literal_directory.display());
        for _ in 0..100 {
            if std::fs::read_to_string(&marker).ok().as_deref() == Some(expected_output.as_str()) {
                break;
            }
            cx.background_executor.timer(Duration::from_millis(20)).await;
        }
        assert_eq!(std::fs::read_to_string(&marker).unwrap(), expected_output);

        // 2b. Verify session exists in isolated daemon
        let list_script = format!(
            "export HOME={home_str:?} XDG_RUNTIME_DIR={runtime_str:?}\n{SHPOOL_SCRIPT_PRELUDE}\n\"$SHPOOL_BIN\" -s \"$SHPOOL_SOCK\" list --json\n"
        );
        let task = project.update(cx, |p, cx| p.exec_script_in_sh(list_script.clone(), cx));
        let mut cmd = task.await.expect("list command creation");
        let output = cmd.output().await.expect("list execution");
        assert!(
            String::from_utf8_lossy(&output.stdout).contains("zed-test-isolated"),
            "isolated daemon must list attached session before kill"
        );

        // Preserve the locator exactly as successful terminal creation does.
        // Then move the current settings to another daemon that owns the SAME
        // session name: a rediscovered kill would terminate the wrong session.
        let original_context = ShpoolSessionContext::new(
            probe.program.clone(), probe.socket.clone(),
        ).unwrap();
        project.update(cx, |project, _| {
            project.terminals.session_contexts.insert("zed-test-isolated".into(), original_context);
        });
        let other_runtime = temp_dir.path().join("other-runtime");
        std::fs::create_dir_all(&other_runtime).unwrap();
        let _other_daemon_guard = IsolatedDaemonGuard { runtime_dir: other_runtime.clone() };
        let other_runtime_str = other_runtime.to_string_lossy().into_owned();
        cx.update(|cx| cx.update_global::<settings::SettingsStore, _>(|store, cx| {
            let config = serde_json::json!({"terminal": {"env": {
                "HOME": home_str.clone(), "XDG_RUNTIME_DIR": other_runtime_str.clone(),
            }}});
            store.set_user_settings(&config.to_string(), cx).unwrap();
        }));
        project.read_with(cx, |project, cx| {
            let path = project.first_project_directory(cx);
            assert_eq!(project.terminal_settings(&path, cx).env.get("XDG_RUNTIME_DIR"),
                Some(&other_runtime_str), "the current settings must actually select daemon B");
        });
        let other_probe_script = format!(
            "export HOME={home_str:?} XDG_RUNTIME_DIR={other_runtime_str:?}\n{SHPOOL_PROBE_SCRIPT}"
        );
        let mut other_command = project.update(cx, |project, cx| {
            project.exec_script_in_sh(other_probe_script, cx)
        }).await.unwrap();
        other_command.kill_on_drop(true);
        let other_output = other_command.output().await.unwrap();
        assert!(other_output.status.success(), "second isolated daemon must start: {}",
            String::from_utf8_lossy(&other_output.stderr));
        let other_probe = parse_shpool_probe(&String::from_utf8_lossy(&other_output.stdout))
            .unwrap().unwrap();
        assert_ne!(other_probe.socket, probe.socket);
        let other_context = ShpoolSessionContext::new(
            other_probe.program.clone(), other_probe.socket.clone(),
        ).unwrap();
        let mut other_attach_args = build_shpool_attach_args(
            &other_probe.socket, None, "zed-test-isolated",
            &Shell::WithArguments { program: "/bin/sh".into(), args: vec!["-i".into()], title_override: None },
        ).unwrap();
        other_attach_args.insert(4, "-b".into());
        let other_arguments = other_attach_args.iter()
            .map(|argument| ShellKind::Posix.try_quote(argument).unwrap())
            .collect::<Vec<_>>().join(" ");
        let other_attach_script = format!(
            "export HOME={home_str:?} XDG_RUNTIME_DIR={other_runtime_str:?}\n{SHPOOL_SCRIPT_PRELUDE}\n\"$SHPOOL_BIN\" {other_arguments}\n"
        );
        let mut other_command = project.update(cx, |project, cx| {
            project.exec_script_in_sh(other_attach_script, cx)
        }).await.unwrap();
        other_command.kill_on_drop(true);
        let other_output = other_command.output().await.unwrap();
        assert!(other_output.status.success(), "same-name session in the second daemon must start: {}",
            String::from_utf8_lossy(&other_output.stderr));

        // 2c. Exercise production kill: kill_persistent_terminal_session
        let kill_res = project
            .update(cx, |p, cx| {
                p.kill_persistent_terminal_session("zed-test-isolated".to_string(), cx)
            })
            .await;
        assert!(kill_res.is_ok(), "production kill must succeed");

        // 2d. Verify session is now absent from isolated daemon
        let task = project.update(cx, |p, cx| p.exec_script_in_sh(list_script, cx));
        let mut cmd = task.await.expect("list command creation");
        let output = cmd.output().await.expect("list execution");
        assert!(
            !String::from_utf8_lossy(&output.stdout).contains("zed-test-isolated"),
            "session must be absent from isolated daemon after production kill"
        );

        // 2e. Production kill on non-existent session must succeed idempotently
        let kill_nonexistent = project
            .update(cx, |p, cx| {
                p.kill_persistent_terminal_session("zed-test-isolated".to_string(), cx)
            })
            .await;
        assert!(
            kill_nonexistent.is_ok(),
            "production kill on non-existent session must succeed idempotently"
        );

        let mut other_command = project.update(cx, |project, cx| {
            project.exec_shpool_context_script(other_context.probe_script(false).unwrap(), cx)
        }).await.unwrap();
        other_command.kill_on_drop(true);
        let other_output = other_command.output().await.unwrap();
        assert!(other_output.status.success());
        let surviving = parse_shpool_probe(&String::from_utf8_lossy(&other_output.stdout))
            .unwrap().unwrap();
        assert!(surviving.sessions.iter().any(|name| name == "zed-test-isolated"),
            "both the initial and idempotent kill must leave the new current daemon untouched");
        let mut other_command = project.update(cx, |project, cx| {
            project.exec_shpool_context_script(
                other_context.command("kill zed-test-isolated").unwrap(), cx,
            )
        }).await.unwrap();
        other_command.kill_on_drop(true);
        assert!(other_command.output().await.unwrap().status.success());

        let unknown_runtime = temp_dir.path().join("unknown-runtime");
        std::fs::create_dir_all(&unknown_runtime).unwrap();
        let _unknown_daemon_guard = IsolatedDaemonGuard { runtime_dir: unknown_runtime.clone() };
        let unknown_runtime_str = unknown_runtime.to_string_lossy().into_owned();
        cx.update(|cx| cx.update_global::<settings::SettingsStore, _>(|store, cx| {
            let config = serde_json::json!({"terminal": {"env": {
                "HOME": home_str.clone(), "XDG_RUNTIME_DIR": unknown_runtime_str.clone(),
            }}});
            store.set_user_settings(&config.to_string(), cx).unwrap();
        }));
        project.read_with(cx, |project, cx| {
            let path = project.first_project_directory(cx);
            assert_eq!(project.terminal_settings(&path, cx).env.get("XDG_RUNTIME_DIR"),
                Some(&unknown_runtime_str));
        });
        let unknown_kill = project.update(cx, |project, cx| {
            project.kill_persistent_terminal_session_in_context("untracked-legacy".into(), None, cx)
        }).await;
        assert!(unknown_kill.is_err(), "a missing unknown legacy session is not proof of termination");
        assert!(!unknown_runtime.join("shpool/shpool.socket").exists(),
            "a destructive legacy probe must not start an unrelated daemon");
        assert!(!unknown_runtime.join("shpool/daemonized-shpool.pid").exists());

        // 3. Strict raw kill verification: rc=1 and exact stderr message
        let raw_kill_script = format!(
            "export HOME={home_str:?} XDG_RUNTIME_DIR={runtime_str:?}\n{SHPOOL_SCRIPT_PRELUDE}\n\"$SHPOOL_BIN\" -s \"$SHPOOL_SOCK\" kill zed-test-isolated\n"
        );
        let raw_task = project.update(cx, |p, cx| p.exec_script_in_sh(raw_kill_script, cx));
        let mut raw_cmd = raw_task.await.expect("raw kill command creation");
        let raw_output = raw_cmd.output().await.expect("raw kill execution");
        let raw_stderr = String::from_utf8_lossy(&raw_output.stderr);
        assert_eq!(
            raw_output.status.code(),
            Some(1),
            "shpool kill of non-existent session must exit with status 1"
        );
        assert!(
            raw_stderr.contains("not found: zed-test-isolated")
                || raw_stderr.contains("no such session: zed-test-isolated"),
            "stderr must report not found for nonexistent session, got: {raw_stderr}"
        );

        // 4. Shutdown daemon explicitly before dropping tempdir
        let pid_path = isolated_runtime
            .join("shpool")
            .join("daemonized-shpool.pid");
        assert!(pid_path.exists(), "daemonized-shpool.pid must exist");
        let pid_str = std::fs::read_to_string(&pid_path).expect("read daemonized-shpool.pid");
        let daemon_pid = pid_str.trim().parse::<u32>().expect("valid pid");

        // Verify that the PID's cmdline contains tempdir before sending any signal
        let ps_output = smol::process::Command::new("/bin/ps")
            .args(["-p", &daemon_pid.to_string(), "-o", "command="])
            .output()
            .await
            .expect("ps command");
        let ps_cmd = String::from_utf8_lossy(&ps_output.stdout);
        assert!(
            ps_cmd.contains(&runtime_str),
            "daemon process command line must contain isolated temp runtime directory"
        );

        // Kill daemon via SIGTERM
        let kill_status = smol::process::Command::new("/bin/kill")
            .args(["-TERM", &daemon_pid.to_string()])
            .output()
            .await;
        assert!(
            kill_status.is_ok(),
            "SIGTERM invocation must not fail to execute"
        );

        // Poll until process is gone
        let mut daemon_dead = false;
        for _ in 0..50 {
            let check = smol::process::Command::new("/bin/kill")
                .args(["-0", &daemon_pid.to_string()])
                .output()
                .await;
            if let Ok(check) = check {
                if !check.status.success() {
                    daemon_dead = true;
                    break;
                }
            }
            cx.background_executor
                .timer(std::time::Duration::from_millis(100))
                .await;
        }
        assert!(
            daemon_dead,
            "daemon PID {daemon_pid} must exit cleanly after SIGTERM"
        );
    }
}
