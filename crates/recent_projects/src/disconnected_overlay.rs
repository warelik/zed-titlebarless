use gpui::{
    AsyncWindowContext, ClickEvent, DismissEvent, EntityId, EventEmitter, FocusHandle, Focusable,
    Render, SharedString, WeakEntity, WindowHandle,
};
use project::project_settings::ProjectSettings;
use remote::RemoteConnectionOptions;
use settings::Settings;
use ui::{ElevationIndex, Modal, ModalFooter, ModalHeader, Section, prelude::*};
use util::ResultExt as _;
use workspace::{
    ModalView, MultiWorkspace, OpenOptions, Workspace, notifications::DetachAndPromptErr,
};

use crate::open_remote_project;

enum Host {
    CollabGuestProject,
    RemoteServerProject(RemoteConnectionOptions, bool, Option<SharedString>),
}

pub struct DisconnectedOverlay {
    workspace: WeakEntity<Workspace>,
    host: Host,
    focus_handle: FocusHandle,
    finished: bool,
}

impl EventEmitter<DismissEvent> for DisconnectedOverlay {}
impl Focusable for DisconnectedOverlay {
    fn focus_handle(&self, _cx: &gpui::App) -> gpui::FocusHandle {
        self.focus_handle.clone()
    }
}
impl ModalView for DisconnectedOverlay {
    fn on_before_dismiss(
        &mut self,
        _window: &mut Window,
        _: &mut Context<Self>,
    ) -> workspace::DismissDecision {
        workspace::DismissDecision::Dismiss(self.finished)
    }
    fn fade_out_background(&self) -> bool {
        true
    }
}

impl DisconnectedOverlay {
    pub fn register(
        workspace: &mut Workspace,
        window: Option<&mut Window>,
        cx: &mut Context<Workspace>,
    ) {
        let Some(window) = window else {
            return;
        };
        cx.subscribe_in(
            workspace.project(),
            window,
            |workspace, _project, event, window, cx| {
                if !matches!(
                    event,
                    project::Event::DisconnectedFromHost
                        | project::Event::DisconnectedFromRemote { .. }
                ) {
                    return;
                }
                let server_not_running = matches!(
                    event,
                    project::Event::DisconnectedFromRemote {
                        server_not_running: true
                    }
                );
                Self::show_for_workspace(workspace, server_not_running, window, cx);
            },
        )
        .detach();
    }

    fn show_for_workspace(
        workspace: &mut Workspace,
        server_not_running: bool,
        window: &mut Window,
        cx: &mut Context<Workspace>,
    ) {
        if workspace.active_modal::<Self>(cx).is_some() {
            return;
        }

        let handle = cx.entity().downgrade();
        let project = workspace.project();
        let remote_connection_options = project.read(cx).remote_connection_options(cx);
        let host = if let Some(remote_connection_options) = remote_connection_options {
            let reason = project
                .read(cx)
                .remote_client()
                .and_then(|remote_client| remote_client.read(cx).disconnect_reason());
            Host::RemoteServerProject(remote_connection_options, server_not_running, reason)
        } else {
            Host::CollabGuestProject
        };

        workspace.toggle_modal(window, cx, |_, cx| DisconnectedOverlay {
            finished: false,
            workspace: handle,
            host,
            focus_handle: cx.focus_handle(),
        });
    }

    fn handle_reconnect(&mut self, _: &ClickEvent, window: &mut Window, cx: &mut Context<Self>) {
        self.finished = true;
        cx.emit(DismissEvent);

        if let Host::RemoteServerProject(remote_connection_options, server_not_running, _) =
            &self.host
        {
            self.reconnect_to_remote_project(
                remote_connection_options.clone(),
                *server_not_running,
                window,
                cx,
            );
        }
    }

    fn reconnect_to_remote_project(
        &self,
        connection_options: RemoteConnectionOptions,
        server_not_running: bool,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) {
        let Some(workspace_entity) = self.workspace.upgrade() else {
            return;
        };

        let Some(window_handle) = window.window_handle().downcast::<MultiWorkspace>() else {
            return;
        };

        let app_state = workspace_entity.read(cx).app_state().clone();
        let paths = workspace_entity
            .read(cx)
            .root_paths(cx)
            .iter()
            .map(|path| path.to_path_buf())
            .collect();
        let workspace_id = workspace_entity.entity_id();
        let workspace = workspace_entity.downgrade();
        drop(workspace_entity);

        cx.spawn_in(window, async move |_, cx| {
            let result = open_remote_project(
                connection_options,
                paths,
                app_state,
                OpenOptions {
                    requesting_window: Some(window_handle),
                    ..Default::default()
                },
                cx,
            )
            .await;

            Self::finish_reconnect_attempt(
                result,
                window_handle,
                workspace,
                workspace_id,
                server_not_running,
                cx,
            )
        })
        .detach_and_prompt_err("Failed to reconnect", window, cx, |_, _, _| None);
    }

    fn finish_reconnect_attempt(
        result: anyhow::Result<WindowHandle<MultiWorkspace>>,
        window_handle: WindowHandle<MultiWorkspace>,
        workspace: WeakEntity<Workspace>,
        workspace_id: EntityId,
        server_not_running: bool,
        cx: &mut AsyncWindowContext,
    ) -> anyhow::Result<()> {
        let is_old_workspace_displayed = window_handle
            .read_with(cx, |multi_workspace, _| {
                multi_workspace.workspace().entity_id() == workspace_id
            })
            .log_err()
            .unwrap_or(false);
        if is_old_workspace_displayed {
            let is_still_disconnected = workspace
                .read_with(cx, |workspace, cx| {
                    workspace
                        .project()
                        .read(cx)
                        .remote_client()
                        .is_some_and(|remote_client| remote_client.read(cx).is_disconnected())
                })
                .log_err()
                .unwrap_or(false);
            if is_still_disconnected {
                workspace
                    .update_in(cx, |workspace, window, cx| {
                        Self::show_for_workspace(workspace, server_not_running, window, cx);
                    })
                    .log_err();
            }
        }

        result?;
        Ok(())
    }

    fn message_for_host(host: &Host, cx: &App) -> String {
        match host {
            Host::CollabGuestProject => {
                "Your connection to the remote project has been lost.".to_string()
            }
            Host::RemoteServerProject(options, server_not_running, reason) => {
                let autosave = if ProjectSettings::get_global(cx)
                    .session
                    .restore_unsaved_buffers
                {
                    "\nUnsaved changes are stored locally."
                } else {
                    ""
                };
                if let Some(reason) = reason {
                    format!(
                        "Your connection to {} has been lost: {reason}.{autosave}",
                        options.display_name(),
                    )
                } else {
                    let reason = if *server_not_running {
                        "process exiting unexpectedly"
                    } else {
                        "not responding"
                    };
                    format!(
                        "Your connection to {} has been lost due to the server {reason}.{autosave}",
                        options.display_name(),
                    )
                }
            }
        }
    }

    fn cancel(&mut self, _: &menu::Cancel, _: &mut Window, cx: &mut Context<Self>) {
        self.finished = true;
        cx.emit(DismissEvent)
    }
}

impl Render for DisconnectedOverlay {
    fn render(&mut self, _: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let can_reconnect = matches!(self.host, Host::RemoteServerProject(..));
        let message = Self::message_for_host(&self.host, cx);

        div()
            .track_focus(&self.focus_handle(cx))
            .elevation_3(cx)
            .on_action(cx.listener(Self::cancel))
            .occlude()
            .w(rems(24.))
            .max_h(rems(40.))
            .child(
                Modal::new("disconnected", None)
                    .header(
                        ModalHeader::new()
                            .show_dismiss_button(true)
                            .child(Headline::new("Disconnected").size(HeadlineSize::Small)),
                    )
                    .section(Section::new().child(Label::new(message)))
                    .footer(
                        ModalFooter::new().end_slot(
                            h_flex()
                                .gap_2()
                                .child(
                                    Button::new("close-window", "Close Window")
                                        .style(ButtonStyle::Filled)
                                        .layer(ElevationIndex::ModalSurface)
                                        .on_click(cx.listener(move |_, _, window, _| {
                                            window.remove_window();
                                        })),
                                )
                                .when(can_reconnect, |el| {
                                    el.child(
                                        Button::new("reconnect", "Reconnect")
                                            .style(ButtonStyle::Filled)
                                            .layer(ElevationIndex::ModalSurface)
                                            .start_icon(Icon::new(IconName::ArrowCircle))
                                            .on_click(cx.listener(Self::handle_reconnect)),
                                    )
                                }),
                        ),
                    ),
            )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use gpui::{Entity, TestAppContext, VisualTestContext};
    use project::Project;
    use remote::RemoteClient;
    use rpc::proto;
    use std::sync::Arc;
    use workspace::AppState;

    #[gpui::test]
    fn test_overlay_message_includes_retained_disconnect_reason(cx: &mut TestAppContext) {
        cx.update(|cx| {
            AppState::test(cx);
            crate::init(cx);
            editor::init(cx);
        });
        let reason = "authentication failed: Permission denied (publickey,password)".to_string();
        let host = Host::RemoteServerProject(
            RemoteConnectionOptions::Ssh(remote::SshConnectionOptions::default()),
            false,
            Some(reason.clone().into()),
        );

        let message = cx.update(|cx| DisconnectedOverlay::message_for_host(&host, cx));
        assert!(message.contains(&reason));
    }

    async fn disconnected_remote_workspace(
        cx: &mut TestAppContext,
        server_cx: &mut TestAppContext,
    ) -> (
        WindowHandle<MultiWorkspace>,
        Entity<Workspace>,
        Entity<RemoteClient>,
        Arc<AppState>,
    ) {
        let app_state = cx.update(|cx| {
            let app_state = AppState::test(cx);
            crate::init(cx);
            editor::init(cx);
            app_state
        });
        cx.update(|cx| {
            release_channel::init(semver::Version::new(0, 0, 0), cx);
        });
        server_cx.update(|cx| {
            release_channel::init(semver::Version::new(0, 0, 0), cx);
        });

        let (options, server_session, connection_guard) = RemoteClient::fake_server(cx, server_cx);
        let ping_handler = server_cx.new(|_| ());
        server_session.add_request_handler::<proto::Ping, _, _, _>(
            ping_handler.downgrade(),
            |_, _, _| async { Ok(proto::Ack {}) },
        );
        drop(connection_guard);
        let remote_client = RemoteClient::connect_mock(options, cx).await;
        let project = cx.update(|cx| {
            Project::remote(
                remote_client.clone(),
                app_state.client.clone(),
                app_state.node_runtime.clone(),
                app_state.user_store.clone(),
                app_state.languages.clone(),
                app_state.fs.clone(),
                false,
                cx,
            )
        });

        let window = cx.add_window({
            let app_state = app_state.clone();
            move |window, cx| {
                let workspace = cx
                    .new(|cx| Workspace::new(None, project.clone(), app_state.clone(), window, cx));
                MultiWorkspace::new(workspace, window, cx)
            }
        });
        let workspace = window
            .read_with(cx, |multi_workspace, _| multi_workspace.workspace().clone())
            .expect("workspace should be present");
        remote_client.update(cx, |remote_client, cx| {
            remote_client.force_server_not_running(cx);
        });
        cx.run_until_parked();
        window
            .update(cx, |multi_workspace, _, cx| {
                let workspace = multi_workspace.workspace().clone();
                if let Some(overlay) = workspace.read(cx).active_modal::<DisconnectedOverlay>(cx) {
                    overlay.update(cx, |overlay, cx| {
                        overlay.finished = true;
                        cx.emit(DismissEvent);
                    });
                }
            })
            .expect("test window should remain open");
        cx.run_until_parked();
        assert!(workspace.read_with(cx, |workspace, cx| {
            workspace.active_modal::<DisconnectedOverlay>(cx).is_none()
        }));

        (window, workspace, remote_client, app_state)
    }

    #[gpui::test]
    async fn test_unchanged_window_completion_reshows_overlay_for_disconnected_workspace(
        cx: &mut TestAppContext,
        server_cx: &mut TestAppContext,
    ) {
        let (window, workspace, remote_client, _) =
            disconnected_remote_workspace(cx, server_cx).await;
        assert!(
            remote_client.read_with(cx, |remote_client, _| { remote_client.is_disconnected() })
        );
        let project = workspace.read_with(cx, |workspace, _| workspace.project().clone());
        project.update(cx, |project, cx| {
            project.disconnected_from_remote(false, cx);
        });
        cx.run_until_parked();
        let event_overlay = workspace
            .read_with(cx, |workspace, cx| {
                workspace.active_modal::<DisconnectedOverlay>(cx)
            })
            .expect("project disconnect event should show the overlay");
        let event_message = event_overlay.read_with(cx, |overlay, cx| {
            DisconnectedOverlay::message_for_host(&overlay.host, cx)
        });
        assert!(event_message.contains("remote server process exited unexpectedly"));
        assert!(!event_message.contains("not responding"));
        event_overlay.update(cx, |overlay, cx| {
            overlay.finished = true;
            cx.emit(DismissEvent);
        });
        cx.run_until_parked();

        let workspace_id = workspace.entity_id();
        let visual_cx = &mut VisualTestContext::from_window(window.into(), cx);
        let mut async_cx = visual_cx.update(|window, cx| window.to_async(cx));

        DisconnectedOverlay::finish_reconnect_attempt(
            Ok(window),
            window,
            workspace.downgrade(),
            workspace_id,
            true,
            &mut async_cx,
        )
        .expect("unchanged-window completion should succeed");
        visual_cx.run_until_parked();

        let overlay = workspace
            .read_with(visual_cx, |workspace, cx| {
                workspace.active_modal::<DisconnectedOverlay>(cx)
            })
            .expect("cancelled reconnect should re-show the overlay");
        let message = overlay.read_with(visual_cx, |overlay, cx| {
            DisconnectedOverlay::message_for_host(&overlay.host, cx)
        });
        assert!(message.contains("remote server process exited unexpectedly"));

        overlay.update(visual_cx, |overlay, cx| {
            overlay.finished = true;
            cx.emit(DismissEvent);
        });
        visual_cx.run_until_parked();
        let mut async_cx = visual_cx.update(|window, cx| window.to_async(cx));
        assert!(
            DisconnectedOverlay::finish_reconnect_attempt(
                Err(anyhow::anyhow!("reconnect failed")),
                window,
                workspace.downgrade(),
                workspace_id,
                true,
                &mut async_cx,
            )
            .is_err()
        );
        visual_cx.run_until_parked();
        assert!(
            workspace
                .read_with(visual_cx, |workspace, cx| {
                    workspace.active_modal::<DisconnectedOverlay>(cx)
                })
                .is_some(),
            "failed reconnect should re-show the overlay on the displayed workspace"
        );
        assert!(remote_client.read_with(visual_cx, |remote_client, _| {
            remote_client.is_disconnected()
        }));
        let overlay = workspace
            .read_with(visual_cx, |workspace, cx| {
                workspace.active_modal::<DisconnectedOverlay>(cx)
            })
            .expect("failed reconnect should leave the overlay available");
        let message = overlay.read_with(visual_cx, |overlay, cx| {
            DisconnectedOverlay::message_for_host(&overlay.host, cx)
        });
        assert!(message.contains("remote server process exited unexpectedly"));
    }

    #[gpui::test]
    async fn test_reconnect_completion_skips_replaced_and_switched_workspaces(
        cx: &mut TestAppContext,
        server_cx: &mut TestAppContext,
    ) {
        let (window, old_workspace, remote_client, app_state) =
            disconnected_remote_workspace(cx, server_cx).await;
        assert!(
            remote_client.read_with(cx, |remote_client, _| { remote_client.is_disconnected() })
        );
        let old_workspace_id = old_workspace.entity_id();
        let (replacement_options, replacement_session, replacement_connection_guard) =
            RemoteClient::fake_server(cx, server_cx);
        let replacement_ping_handler = server_cx.new(|_| ());
        replacement_session.add_request_handler::<proto::Ping, _, _, _>(
            replacement_ping_handler.downgrade(),
            |_, _, _| async { Ok(proto::Ack {}) },
        );
        drop(replacement_connection_guard);
        let replacement_remote_client = RemoteClient::connect_mock(replacement_options, cx).await;
        let replacement_project = cx.update(|cx| {
            Project::remote(
                replacement_remote_client.clone(),
                app_state.client.clone(),
                app_state.node_runtime.clone(),
                app_state.user_store.clone(),
                app_state.languages.clone(),
                app_state.fs.clone(),
                false,
                cx,
            )
        });
        let switched_project = Project::test(fs::FakeFs::new(cx.executor()), [], cx).await;
        let visual_cx = &mut VisualTestContext::from_window(window.into(), cx);
        let replacement_workspace = window
            .update(visual_cx, |multi_workspace, window, cx| {
                let workspace = cx.new(|cx| {
                    Workspace::new(
                        None,
                        replacement_project.clone(),
                        app_state.clone(),
                        window,
                        cx,
                    )
                });
                multi_workspace.activate(workspace.clone(), None, window, cx);
                workspace
            })
            .expect("replacement workspace should activate");
        let focused_before_success = visual_cx.update(|window, cx| window.focused(cx));
        let mut async_cx = visual_cx.update(|window, cx| window.to_async(cx));

        DisconnectedOverlay::finish_reconnect_attempt(
            Ok(window),
            window,
            old_workspace.downgrade(),
            old_workspace_id,
            true,
            &mut async_cx,
        )
        .expect("successful reconnect should complete");
        visual_cx.run_until_parked();

        assert!(remote_client.read_with(visual_cx, |remote_client, _| {
            remote_client.is_disconnected()
        }));
        assert!(
            !replacement_remote_client.read_with(visual_cx, |remote_client, _| {
                remote_client.is_disconnected()
            })
        );
        assert_eq!(
            window
                .read_with(visual_cx, |multi_workspace, _| {
                    multi_workspace.workspace().entity_id()
                })
                .expect("window should remain open"),
            replacement_workspace.entity_id()
        );
        assert!(old_workspace.read_with(visual_cx, |workspace, cx| {
            workspace.active_modal::<DisconnectedOverlay>(cx).is_none()
        }));
        assert!(replacement_workspace.read_with(visual_cx, |workspace, cx| {
            workspace.active_modal::<DisconnectedOverlay>(cx).is_none()
        }));
        assert_eq!(
            visual_cx.update(|window, cx| window.focused(cx)),
            focused_before_success,
            "a successful workspace replacement must not steal focus"
        );

        let switched_workspace = window
            .update(visual_cx, |multi_workspace, window, cx| {
                let workspace = cx.new(|cx| {
                    Workspace::new(
                        None,
                        switched_project.clone(),
                        app_state.clone(),
                        window,
                        cx,
                    )
                });
                multi_workspace.activate(workspace.clone(), None, window, cx);
                workspace
            })
            .expect("switched workspace should activate");
        let mut async_cx = visual_cx.update(|window, cx| window.to_async(cx));

        assert!(
            DisconnectedOverlay::finish_reconnect_attempt(
                Err(anyhow::anyhow!("reconnect failed")),
                window,
                old_workspace.downgrade(),
                old_workspace_id,
                true,
                &mut async_cx,
            )
            .is_err()
        );
        visual_cx.run_until_parked();

        assert_eq!(
            window
                .read_with(visual_cx, |multi_workspace, _| {
                    multi_workspace.workspace().entity_id()
                })
                .expect("window should remain open"),
            switched_workspace.entity_id()
        );
        assert!(old_workspace.read_with(visual_cx, |workspace, cx| {
            workspace.active_modal::<DisconnectedOverlay>(cx).is_none()
        }));
        assert!(switched_workspace.read_with(visual_cx, |workspace, cx| {
            workspace.active_modal::<DisconnectedOverlay>(cx).is_none()
        }));
    }
}
