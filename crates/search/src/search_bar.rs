use editor::{Editor, EditorElement, EditorStyle, MultiBufferOffset, ToOffset};
use gpui::{Action, App, Entity, FocusHandle, Hsla, IntoElement, TextStyle};
use settings::Settings;
use theme_settings::ThemeSettings;
use ui::{IconButton, IconButtonShape};
use ui::{Tooltip, prelude::*};
use workspace::Toolbar;

pub(super) enum HistoryNavigationDirection {
    Previous,
    Next,
}

pub(super) fn should_navigate_history(
    editor: &Entity<Editor>,
    direction: HistoryNavigationDirection,
    cx: &App,
) -> bool {
    let editor_ref = editor.read(cx);
    let snapshot = editor_ref.buffer().read(cx).snapshot(cx);
    if snapshot.max_point().row == 0 {
        return true;
    }
    let selections = editor_ref.selections.disjoint_anchors();
    if let [selection] = selections {
        let offset = selection.end.to_offset(&snapshot);
        match direction {
            HistoryNavigationDirection::Previous => offset == MultiBufferOffset(0),
            HistoryNavigationDirection::Next => offset == snapshot.len(),
        }
    } else {
        true
    }
}

pub(super) enum ActionButtonState {
    Disabled,
    Toggled,
}

pub(super) fn render_action_button(
    id_prefix: &'static str,
    icon: ui::IconName,
    button_state: Option<ActionButtonState>,
    tooltip: &'static str,
    action: &'static dyn Action,
    focus_handle: FocusHandle,
) -> impl IntoElement {
    IconButton::new(
        SharedString::from(format!("{id_prefix}-{}", action.name())),
        icon,
    )
    .shape(IconButtonShape::Square)
    .on_click({
        let focus_handle = focus_handle.clone();
        move |_, window, cx| {
            if !focus_handle.is_focused(window) {
                window.focus(&focus_handle, cx);
            }
            window.dispatch_action(action.boxed_clone(), cx);
        }
    })
    .tooltip(move |_window, cx| Tooltip::for_action_in(tooltip, action, &focus_handle, cx))
    .when_some(button_state, |this, state| match state {
        ActionButtonState::Toggled => this.toggle_state(true),
        ActionButtonState::Disabled => this.disabled(true),
    })
}

/// Wraps a search editor. The vertical padding of stock rows does not fit a compact row, and
/// the editor is centred in the row either way.
pub(crate) fn text_input_cell(cx: &App) -> Div {
    div()
        .flex_1()
        .when(Toolbar::compact_row_height(cx).is_none(), |this| {
            this.py_1()
        })
}

/// A search error shown under its input row. Stock tucks it 4px under the row (`mt_neg_1`
/// against the stack's `gap_2`). A compact stack has no gap, so there the message gets a row of
/// its own, as tall as the input rows, with the text centred in it.
pub(crate) fn error_line(message: impl Into<SharedString>, cx: &App) -> AnyElement {
    let label = Label::new(message)
        .size(LabelSize::Small)
        .color(Color::Error)
        .ml_2();
    match Toolbar::compact_row_height(cx) {
        Some(_) => v_flex()
            .min_h(Toolbar::row_height(cx))
            .justify_center()
            .child(label)
            .into_any_element(),
        None => label.mt_neg_1().into_any_element(),
    }
}

pub(crate) fn input_base_styles(border_color: Hsla, map: impl FnOnce(Div) -> Div, cx: &App) -> Div {
    h_flex()
        .map(map)
        .min_w_32()
        .min_h(Toolbar::row_height(cx))
        .pl_2()
        .pr_1()
        .border_1()
        .border_color(border_color)
        .rounded_md()
}
pub(crate) fn filter_search_results_input(
    border_color: Hsla,
    map: impl FnOnce(Div) -> Div,
    cx: &App,
) -> Div {
    input_base_styles(border_color, map, cx).pl_0().child(
        h_flex()
            .mr_2()
            .px_2()
            .h_full()
            .border_r_1()
            .border_color(cx.theme().colors().border)
            .bg(cx.theme().colors().text_accent.opacity(0.05))
            .child(Label::new("Find in Results").color(Color::Muted)),
    )
}

pub(crate) fn render_text_input(
    editor: &Entity<Editor>,
    color_override: Option<Color>,
    app: &App,
) -> impl IntoElement {
    let (color, use_syntax) = if editor.read(app).read_only(app) {
        (app.theme().colors().text_disabled, false)
    } else {
        match color_override {
            Some(color_override) => (color_override.color(app), false),
            None => (app.theme().colors().text, true),
        }
    };

    let settings = ThemeSettings::get_global(app);
    let text_style = TextStyle {
        color,
        font_family: settings.buffer_font.family.clone(),
        font_features: settings.buffer_font.features.clone(),
        font_fallbacks: settings.buffer_font.fallbacks.clone(),
        font_size: rems(0.875).into(),
        font_weight: settings.buffer_font.weight,
        line_height: relative(1.3),
        ..TextStyle::default()
    };

    let mut editor_style = EditorStyle {
        background: app.theme().colors().toolbar_background,
        local_player: app.theme().players().local(),
        text: text_style,
        ..EditorStyle::default()
    };
    if use_syntax {
        editor_style.syntax = app.theme().syntax().clone();
    }

    EditorElement::new(editor, editor_style)
}

/// This element makes all search inputs align as if they were in the same column
pub(crate) fn alignment_element() -> Div {
    div().size_5().flex_none().ml_0p5()
}
