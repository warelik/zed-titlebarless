use super::*;
use gpui::ScrollHandle;
use ui::{IconButtonShape, Indicator, TabCloseSide, TabPosition};
use workspace::TabBarSettings;
use workspace::item::{ClosePosition, ItemSettings, ShowCloseButton};

#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum TabsRowKind {
    Projects,
    Threads,
    /// Not a row: the Add Project button, which sits with the agent panel's own buttons.
    AddProject,
}

/// One of the two tab rows that stand in for the sidebar at the top of the agent panel. It
/// renders from the sidebar's entries, so the rows and the sidebar share one model.
pub(crate) struct ThreadTabsRow {
    sidebar: WeakEntity<Sidebar>,
    kind: TabsRowKind,
    scroll_handle: ScrollHandle,
    scrolled_to: Option<usize>,
    _observe_sidebar: gpui::Subscription,
}

impl ThreadTabsRow {
    pub(crate) fn new(
        sidebar: &Entity<Sidebar>,
        kind: TabsRowKind,
        cx: &mut Context<Self>,
    ) -> Self {
        Self {
            sidebar: sidebar.downgrade(),
            kind,
            scroll_handle: ScrollHandle::new(),
            scrolled_to: None,
            _observe_sidebar: cx.observe(sidebar, |_, _, cx| cx.notify()),
        }
    }
}

impl Render for ThreadTabsRow {
    fn render(&mut self, window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let Some(sidebar) = self.sidebar.upgrade() else {
            return div().into_any_element();
        };
        let kind = self.kind;
        let row = sidebar.update(cx, |sidebar, cx| match kind {
            TabsRowKind::Projects => Ok(sidebar.render_project_tabs(window, cx)),
            TabsRowKind::Threads => Ok(sidebar.render_thread_tabs(window, cx)),
            TabsRowKind::AddProject => Err(sidebar
                .render_recent_projects_button(true, cx)
                .into_any_element()),
        });
        let row = match row {
            Ok(row) => row,
            Err(button) => return button,
        };

        // Like the editor's tab bar, bring the active tab into view when it changes.
        if row.active_index != self.scrolled_to {
            if let Some(index) = row.active_index {
                self.scroll_handle.scroll_to_item(index);
            }
            self.scrolled_to = row.active_index;
        }

        let filler = div()
            .id("tab_bar_filler")
            .min_w_6()
            .h(Tab::container_height(cx))
            .flex_grow_1()
            .child("");
        let filler = if workspace::title_bar_in_status_bar(cx) {
            workspace::move_window_on_drag(filler, window, cx)
        } else {
            filler
        };

        h_flex()
            .id(match kind {
                TabsRowKind::Projects => "agent_panel_project_tabs",
                TabsRowKind::Threads | TabsRowKind::AddProject => "agent_panel_thread_tabs",
            })
            .overflow_x_scroll()
            .w_full()
            .track_scroll(&self.scroll_handle)
            .children(row.tabs)
            .child(filler)
            .into_any_element()
    }
}

pub(crate) struct RenderedTabsRow {
    tabs: Vec<AnyElement>,
    active_index: Option<usize>,
}

fn tab_position(index: usize, count: usize, active_index: Option<usize>) -> TabPosition {
    if index == 0 {
        TabPosition::First
    } else if index + 1 == count {
        TabPosition::Last
    } else {
        TabPosition::Middle(active_index.map_or(Ordering::Equal, |active| index.cmp(&active)))
    }
}

/// The `tabs.*` settings the editor's tabs follow, copied so that rendering can keep using
/// `cx` mutably.
#[derive(Clone, Copy)]
struct TabSettings {
    close_side: TabCloseSide,
    show_close_button: ShowCloseButton,
    min_width: Option<Pixels>,
    max_width: Option<Pixels>,
}

impl TabSettings {
    fn get(cx: &App) -> Self {
        let settings = ItemSettings::get_global(cx);
        Self {
            close_side: match settings.close_position {
                ClosePosition::Left => TabCloseSide::Start,
                ClosePosition::Right => TabCloseSide::End,
            },
            show_close_button: settings.show_close_button,
            min_width: settings.min_width,
            max_width: settings.max_width,
        }
    }
}

fn tab_label(title: SharedString, is_active: bool, settings: TabSettings) -> AnyElement {
    let truncate_middle = settings.max_width.is_some();
    div()
        .when(truncate_middle, |this| this.flex_1().min_w_0())
        .child(
            Label::new(title)
                .single_line()
                .color(if is_active {
                    Color::Default
                } else {
                    Color::Muted
                })
                .when(truncate_middle, |this| this.truncate_middle()),
        )
        .into_any_element()
}

/// Wraps a tab's icon and label the way the editor's tabs do, including the width bounds.
fn tab_content(
    id: impl Into<ElementId>,
    tooltip: SharedString,
    children: Vec<AnyElement>,
    settings: TabSettings,
) -> impl IntoElement {
    h_flex()
        .id(id)
        .gap_1()
        .when(
            settings.min_width.is_some() || settings.max_width.is_some(),
            |this| this.flex_grow(1.),
        )
        .when(settings.max_width.is_some(), |this| {
            this.min_w_0().overflow_hidden()
        })
        .children(children)
        .tooltip(Tooltip::text(tooltip))
}

/// The close button of a closable tab, shown as `tabs.show_close_button` asks.
fn close_button(
    id: impl Into<ElementId>,
    icon: IconName,
    tooltip: &'static str,
    settings: TabSettings,
) -> Option<IconButton> {
    let button = match settings.show_close_button {
        ShowCloseButton::Always => IconButton::new(id, icon),
        ShowCloseButton::Hover => IconButton::new(id, icon).visible_on_hover(""),
        ShowCloseButton::Hidden => return None,
    };
    Some(
        button
            .shape(IconButtonShape::Square)
            .icon_color(Color::Muted)
            .size(ButtonSize::None)
            .icon_size(IconSize::Small)
            .tooltip(Tooltip::text(tooltip)),
    )
}

enum CloseTab {
    StopThread(ThreadId),
    RemoveDraft(ThreadId, ThreadEntryWorkspace),
    ArchiveThread(acp::SessionId),
    CloseTerminal(TerminalThreadMetadata, ThreadEntryWorkspace),
}

impl Sidebar {
    pub(crate) fn render_project_tabs(
        &mut self,
        _window: &mut Window,
        cx: &mut Context<Self>,
    ) -> RenderedTabsRow {
        let chip = TabBarSettings::get_global(cx).pinned_tab_chips;
        let headers = self
            .contents
            .project_header_indices
            .iter()
            .filter_map(|&entry_ix| match self.contents.entries.get(entry_ix)? {
                ListEntry::ProjectHeader {
                    key,
                    label,
                    has_running_threads,
                    waiting_thread_count,
                    has_notifications,
                    is_active,
                    has_threads,
                    ..
                } => Some((
                    entry_ix,
                    key.clone(),
                    label.clone(),
                    *has_running_threads,
                    *waiting_thread_count,
                    *has_notifications,
                    *is_active,
                    *has_threads,
                )),
                ListEntry::Thread(_) | ListEntry::Terminal(_) => None,
            })
            .collect::<Vec<_>>();

        let count = headers.len();
        let active_index = headers.iter().position(|header| header.6);
        let tabs = headers
            .into_iter()
            .enumerate()
            .map(
                |(
                    index,
                    (
                        entry_ix,
                        key,
                        label,
                        has_running_threads,
                        waiting_thread_count,
                        has_notifications,
                        is_active,
                        has_threads,
                    ),
                )| {
                    let settings = TabSettings::get(cx);
                    let indicator = if has_running_threads {
                        Some(
                            Icon::new(IconName::LoadCircle)
                                .size(IconSize::XSmall)
                                .color(Color::Muted)
                                .with_rotate_animation(2)
                                .into_any_element(),
                        )
                    } else if waiting_thread_count > 0 {
                        Some(
                            Icon::new(IconName::Warning)
                                .size(IconSize::XSmall)
                                .color(Color::Warning)
                                .into_any_element(),
                        )
                    } else if has_notifications {
                        Some(Indicator::dot().color(Color::Accent).into_any_element())
                    } else {
                        None
                    };

                    self.project_header_menu_handles
                        .entry(entry_ix)
                        .or_default();
                    let menu_handle = self
                        .project_header_menu_handles
                        .get(&entry_ix)
                        .cloned()
                        .unwrap_or_default();
                    let menu = self.render_project_header_ellipsis_menu(
                        entry_ix,
                        "tab-",
                        &key,
                        is_active,
                        has_threads,
                        &SharedString::from(""),
                        true,
                        cx,
                    );

                    let tooltip: SharedString = key
                        .path_list()
                        .paths()
                        .iter()
                        .map(|path| path.display().to_string())
                        .join(", ")
                        .into();
                    let mut children = vec![tab_label(label, is_active, settings)];
                    children.extend(self.render_remote_project_icon(entry_ix, key.host().as_ref()));

                    Tab::new(("project-tab", index))
                        .position(tab_position(index, count, active_index))
                        .close_side(settings.close_side)
                        .toggle_state(is_active)
                        .chip(chip)
                        .min_width(settings.min_width)
                        .max_width(settings.max_width)
                        .on_click(cx.listener({
                            move |this, _: &ClickEvent, window, cx| {
                                this.activate_project_tab(&key, window, cx);
                            }
                        }))
                        .on_mouse_down(gpui::MouseButton::Right, move |_, window, cx| {
                            cx.stop_propagation();
                            menu_handle.toggle(window, cx);
                        })
                        .start_slot::<AnyElement>(indicator)
                        .end_slot(menu)
                        .child(tab_content(
                            ("project-tab-content", index),
                            tooltip,
                            children,
                            settings,
                        ))
                        .into_any_element()
                },
            )
            .collect();

        RenderedTabsRow { tabs, active_index }
    }

    pub(crate) fn ordered_thread_tab_entries(&self) -> Vec<(usize, ListEntry)> {
        let entries = &self.contents.entries;
        let Some(active_header) = entries.iter().position(|entry| {
            matches!(
                entry,
                ListEntry::ProjectHeader {
                    is_active: true,
                    ..
                }
            )
        }) else {
            return Vec::new();
        };

        // Tabs keep the order they were opened in, like editor tabs, instead of the sidebar's
        // most-recent-first order, which would move a tab whenever its thread is used.
        entries
            .iter()
            .enumerate()
            .skip(active_header + 1)
            .take_while(|(_, entry)| !matches!(entry, ListEntry::ProjectHeader { .. }))
            .map(|(entry_ix, entry)| (entry_ix, entry.clone()))
            .sorted_by_key(|(_, entry)| {
                let is_empty_draft = matches!(
                    entry,
                    ListEntry::Thread(thread) if thread.draft == Some(DraftKind::Empty)
                );
                let created_at = match entry {
                    ListEntry::Thread(thread) => thread
                        .metadata
                        .created_at
                        .unwrap_or(DateTime::<Utc>::UNIX_EPOCH),
                    ListEntry::Terminal(terminal) => terminal.metadata.created_at,
                    ListEntry::ProjectHeader { .. } => DateTime::<Utc>::MAX_UTC,
                };
                let id_key = match entry {
                    ListEntry::Thread(thread) => thread.metadata.thread_id.to_key_string(),
                    ListEntry::Terminal(terminal) => terminal.metadata.terminal_id.to_string(),
                    ListEntry::ProjectHeader { .. } => String::new(),
                };
                (is_empty_draft, created_at, id_key)
            })
            .collect()
    }

    pub(crate) fn render_thread_tabs(
        &mut self,
        _window: &mut Window,
        cx: &mut Context<Self>,
    ) -> RenderedTabsRow {
        let chip = TabBarSettings::get_global(cx).unpinned_tab_chips;
        let rows = self.ordered_thread_tab_entries();

        let count = rows.len();
        let active_index = rows.iter().position(|(_, entry)| {
            self.active_entry
                .as_ref()
                .is_some_and(|active| active.matches_entry(entry))
        });
        let tabs = rows
            .into_iter()
            .enumerate()
            .filter_map(|(index, (entry_ix, entry))| {
                let position = tab_position(index, count, active_index);
                let is_active = active_index == Some(index);
                match entry {
                    ListEntry::Thread(thread) => Some(self.render_thread_tab(
                        index, entry_ix, &thread, position, is_active, chip, cx,
                    )),
                    ListEntry::Terminal(terminal) => Some(self.render_terminal_tab(
                        index, entry_ix, &terminal, position, is_active, chip, cx,
                    )),
                    ListEntry::ProjectHeader { .. } => None,
                }
            })
            .collect();

        RenderedTabsRow { tabs, active_index }
    }

    fn activate_project_tab(
        &mut self,
        key: &ProjectGroupKey,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) {
        self.activate_or_open_workspace_for_group_and_open_agent_panel(key, window, cx);
    }

    fn close_tab(&mut self, close: &CloseTab, window: &mut Window, cx: &mut Context<Self>) {
        match close {
            CloseTab::StopThread(thread_id) => self.stop_thread(thread_id, cx),
            CloseTab::RemoveDraft(thread_id, workspace) => {
                self.remove_draft(*thread_id, workspace, window, cx)
            }
            CloseTab::ArchiveThread(session_id) => self.archive_thread(session_id, window, cx),
            CloseTab::CloseTerminal(metadata, workspace) => {
                self.close_terminal(metadata, workspace, window, cx)
            }
        }
    }

    /// Leaves the history the agent panel shows in place of the thread.
    fn leave_history(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        if matches!(self.view, SidebarView::Archive(_)) {
            self.show_thread_list(window, cx);
        }
    }

    fn render_thread_tab(
        &mut self,
        index: usize,
        entry_ix: usize,
        thread: &Arc<ThreadEntry>,
        position: TabPosition,
        is_active: bool,
        chip: bool,
        cx: &mut Context<Self>,
    ) -> AnyElement {
        let settings = TabSettings::get(cx);
        let thread_id = thread.metadata.thread_id;
        let title = thread.metadata.display_title();
        let is_running = matches!(
            thread.status,
            AgentThreadStatus::Running
                | AgentThreadStatus::WaitingForConfirmation
                | AgentThreadStatus::Unconfirmed
        );

        let indicator = match thread.status {
            AgentThreadStatus::Running => Some(
                Icon::new(IconName::LoadCircle)
                    .size(IconSize::XSmall)
                    .color(Color::Muted)
                    .with_rotate_animation(2)
                    .into_any_element(),
            ),
            AgentThreadStatus::WaitingForConfirmation => Some(
                Icon::new(IconName::Warning)
                    .size(IconSize::XSmall)
                    .color(Color::Warning)
                    .into_any_element(),
            ),
            AgentThreadStatus::Unconfirmed => Some(
                Icon::new(IconName::Info)
                    .size(IconSize::XSmall)
                    .color(Color::Muted)
                    .into_any_element(),
            ),
            AgentThreadStatus::Error => {
                Some(Indicator::dot().color(Color::Error).into_any_element())
            }
            AgentThreadStatus::Completed if self.contents.is_thread_notified(&thread_id) => {
                Some(Indicator::dot().color(Color::Accent).into_any_element())
            }
            AgentThreadStatus::Completed => None,
        };

        let icon = if thread.draft.is_some() {
            Icon::new(IconName::Circle)
                .size(IconSize::Small)
                .color(Color::Custom(cx.theme().colors().icon_muted.opacity(0.2)))
        } else if let Some(svg) = thread.icon_from_external_svg.clone() {
            Icon::from_external_svg(svg)
                .size(IconSize::Small)
                .color(Color::Muted)
        } else {
            Icon::new(thread.icon)
                .size(IconSize::Small)
                .color(Color::Muted)
        };

        let is_renaming = self.rename_target == Some(RenameTarget::Thread(thread_id));
        let label = if is_renaming {
            self.render_rename_title_editor(cx)
        } else {
            tab_label(title.clone(), is_active, settings)
        };

        let close = if is_running {
            Some((
                CloseTab::StopThread(thread_id),
                IconName::Stop,
                "Stop Generation",
            ))
        } else {
            match (&thread.draft, thread.metadata.session_id.clone()) {
                (Some(_), _) => Some((
                    CloseTab::RemoveDraft(thread_id, thread.workspace.clone()),
                    IconName::Close,
                    "Discard Draft",
                )),
                (None, Some(session_id)) => Some((
                    CloseTab::ArchiveThread(session_id),
                    IconName::Close,
                    "Archive Thread",
                )),
                (None, None) => None,
            }
        };
        let close = close.map(Rc::new);
        let close_button = close.as_ref().and_then(|close| {
            let (_, icon, tooltip) = close.as_ref();
            close_button(("close-thread-tab", index), *icon, tooltip, settings).map(|button| {
                let close = close.clone();
                button.on_click(cx.listener(move |this, _, window, cx| {
                    this.close_tab(&close.0, window, cx);
                }))
            })
        });

        let metadata = thread.metadata.clone();
        let thread_workspace = thread.workspace.clone();
        let tab = Tab::new(("thread-tab", index))
            .position(position)
            .close_side(settings.close_side)
            .toggle_state(is_active)
            .chip(chip)
            .min_width(settings.min_width)
            .max_width(settings.max_width)
            .on_click(cx.listener({
                let title = title.clone();
                move |this, event: &ClickEvent, window, cx| {
                    if event.click_count() > 1 {
                        this.start_renaming_entry(
                            entry_ix,
                            RenameTarget::Thread(thread_id),
                            title.clone(),
                            window,
                            cx,
                        );
                        return;
                    }
                    this.leave_history(window, cx);
                    this.selection = None;
                    match &thread_workspace {
                        ThreadEntryWorkspace::Open(workspace) => {
                            this.activate_thread(metadata.clone(), workspace, false, window, cx);
                        }
                        ThreadEntryWorkspace::Closed {
                            folder_paths,
                            project_group_key,
                        } => {
                            this.open_workspace_and_activate_thread(
                                metadata.clone(),
                                folder_paths.clone(),
                                project_group_key,
                                window,
                                cx,
                            );
                        }
                    }
                }
            }))
            .on_aux_click(cx.listener({
                let close = close.clone();
                move |this, event: &ClickEvent, window, cx| {
                    if !event.is_middle_click() {
                        return;
                    }
                    if let Some(close) = &close
                        && !matches!(close.0, CloseTab::StopThread(_))
                    {
                        this.close_tab(&close.0, window, cx);
                    }
                    cx.stop_propagation();
                }
            }))
            .start_slot::<AnyElement>(indicator)
            .end_slot::<IconButton>(close_button)
            .child(tab_content(
                ("thread-tab-content", index),
                title,
                vec![icon.into_any_element(), label],
                settings,
            ));

        match self.thread_context_menu(entry_ix, thread, cx) {
            Some(menu) => right_click_menu(("thread-tab-menu", index))
                .trigger(move |_, _, _| tab)
                .menu(menu)
                .into_any_element(),
            None => tab.into_any_element(),
        }
    }

    fn render_terminal_tab(
        &mut self,
        index: usize,
        entry_ix: usize,
        terminal: &TerminalEntry,
        position: TabPosition,
        is_active: bool,
        chip: bool,
        cx: &mut Context<Self>,
    ) -> AnyElement {
        let settings = TabSettings::get(cx);
        let terminal_id = terminal.metadata.terminal_id;
        let display_title = terminal.metadata.display_title();
        let (icon, title) = match split_leading_icon_char(&display_title, &[]) {
            Some((icon_char, title, _)) => (
                Label::new(icon_char)
                    .size(LabelSize::Small)
                    .color(Color::Muted)
                    .into_any_element(),
                title,
            ),
            None => (
                Icon::new(IconName::Terminal)
                    .size(IconSize::Small)
                    .color(Color::Muted)
                    .into_any_element(),
                display_title.clone(),
            ),
        };

        let is_renaming = self.rename_target == Some(RenameTarget::Terminal(terminal_id));
        let label = if is_renaming {
            self.render_rename_title_editor(cx)
        } else {
            tab_label(title, is_active, settings)
        };
        let indicator = terminal
            .has_notification
            .then(|| Indicator::dot().color(Color::Accent));

        let close = Rc::new(CloseTab::CloseTerminal(
            terminal.metadata.clone(),
            terminal.workspace.clone(),
        ));
        let close_button = close_button(
            ("close-terminal-tab", index),
            IconName::Close,
            "Close Terminal",
            settings,
        )
        .map(|button| {
            let close = close.clone();
            button.on_click(cx.listener(move |this, _, window, cx| {
                this.close_tab(&close, window, cx);
            }))
        });

        let metadata = terminal.metadata.clone();
        let terminal_workspace = terminal.workspace.clone();
        let rename_title = terminal.metadata.editable_title();
        let tab = Tab::new(("terminal-tab", index))
            .position(position)
            .close_side(settings.close_side)
            .toggle_state(is_active)
            .chip(chip)
            .min_width(settings.min_width)
            .max_width(settings.max_width)
            .on_click(cx.listener(move |this, event: &ClickEvent, window, cx| {
                if event.click_count() > 1 {
                    this.start_renaming_entry(
                        entry_ix,
                        RenameTarget::Terminal(terminal_id),
                        rename_title.clone(),
                        window,
                        cx,
                    );
                    return;
                }
                this.leave_history(window, cx);
                this.selection = None;
                this.activate_terminal_entry(
                    metadata.clone(),
                    terminal_workspace.clone(),
                    false,
                    window,
                    cx,
                );
            }))
            .on_aux_click(cx.listener(move |this, event: &ClickEvent, window, cx| {
                if !event.is_middle_click() {
                    return;
                }
                this.close_tab(&close, window, cx);
                cx.stop_propagation();
            }))
            .start_slot::<Indicator>(indicator)
            .end_slot::<IconButton>(close_button)
            .child(tab_content(
                ("terminal-tab-content", index),
                display_title,
                vec![icon, label],
                settings,
            ));

        right_click_menu(("terminal-tab-menu", index))
            .trigger(move |_, _, _| tab)
            .menu(Self::terminal_context_menu(entry_ix, terminal, cx))
            .into_any_element()
    }
}
