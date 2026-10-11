use std::ops::Range;

use gpui::{AnyElement, ScrollHandle};
use smallvec::SmallVec;

use crate::Tab;
use crate::prelude::*;

/// Minimum width reserved at the end of each tab row for the empty tab bar filler (`.min_w_6()`).
pub const TAB_BAR_FILLER_MIN_WIDTH: Pixels = px(24.);

/// Returns how many tabs of `min_width` fit into `row_width` alongside `filler_width`.
/// Always returns at least `1`.
pub fn tab_row_capacity(row_width: Pixels, min_width: Pixels, filler_width: Pixels) -> usize {
    if min_width <= px(0.) {
        return usize::MAX;
    }
    let available = (row_width - filler_width).max(px(0.));
    ((available / min_width).floor() as usize).max(1)
}

/// Partitions `0..count` tab indices across up to `max_rows` rows.
/// - Row 0 takes up to `first_row_capacity.max(1)` tabs.
/// - Rows `1..max_rows - 1` take up to `row_capacity.max(1)` tabs each.
/// - The last allowed row (`max_rows - 1`) takes all remaining tabs (`start..count`).
/// - Never returns empty trailing rows; when `count == 0`, returns `vec![0..0]`.
pub fn tab_row_ranges(
    count: usize,
    first_row_capacity: usize,
    row_capacity: usize,
    max_rows: usize,
) -> Vec<Range<usize>> {
    let max_rows = max_rows.max(1);
    if count == 0 || max_rows == 1 {
        return vec![0..count];
    }
    let first_cap = first_row_capacity.max(1);
    let full_cap = row_capacity.max(1);
    let mut ranges = Vec::with_capacity(max_rows);
    let mut start = 0;
    for row_ix in 0..max_rows {
        if start >= count {
            break;
        }
        if row_ix + 1 == max_rows {
            ranges.push(start..count);
            break;
        }
        let cap = if row_ix == 0 { first_cap } else { full_cap };
        let end = (start + cap).min(count);
        ranges.push(start..end);
        start = end;
    }
    ranges
}

/// Tracks measured widths for wrapped tab rows and notifies only when width changes by > 1px.
#[derive(Debug, Default, Clone, Copy, PartialEq)]
pub struct TabRows {
    pub first_row_width: Option<Pixels>,
    pub full_row_width: Option<Pixels>,
}

impl TabRows {
    pub const REMEASURE_EPSILON: Pixels = px(1.);

    pub fn update_widths(
        &mut self,
        first_row_width: Option<Pixels>,
        full_row_width: Option<Pixels>,
    ) -> bool {
        // Store a positive measurement only when it moves by more than the epsilon.
        fn update(stored: &mut Option<Pixels>, measured: Option<Pixels>) -> bool {
            let Some(new) = measured.filter(|&width| width > px(0.)) else {
                return false;
            };
            let changed = stored.is_none_or(|old| (old - new).abs() > TabRows::REMEASURE_EPSILON);
            if changed {
                *stored = Some(new);
            }
            changed
        }
        let changed_first = update(&mut self.first_row_width, first_row_width);
        let changed_full = update(&mut self.full_row_width, full_row_width);
        changed_first || changed_full
    }

    pub fn ranges(
        &self,
        count: usize,
        min_width: Option<Pixels>,
        filler_width: Pixels,
        max_rows: usize,
    ) -> Vec<Range<usize>> {
        let max_rows = max_rows.max(1);
        let Some(min_width) = min_width.filter(|&w| w > px(0.)) else {
            return vec![0..count];
        };
        if max_rows <= 1 || count <= 1 {
            return vec![0..count];
        }
        let Some(first_w) = self.first_row_width.filter(|&w| w > px(0.)) else {
            return vec![0..count];
        };
        let full_w = self
            .full_row_width
            .filter(|&w| w > px(0.))
            .unwrap_or(first_w);
        let first_cap = tab_row_capacity(first_w, min_width, filler_width);
        let full_cap = tab_row_capacity(full_w, min_width, filler_width);
        tab_row_ranges(count, first_cap, full_cap, max_rows)
    }

    /// Wraps a rendered tab element (which may be wrapped in `RightClickMenu`) in a block `Div`
    /// with explicit `min_w` and `max_w` + `overflow_hidden` so Taffy's flexbox layout shrinks
    /// tabs proportionally down to `min_width` before overflowing.
    pub fn wrap_tab(tab: AnyElement, min_width: Pixels, max_width: Pixels) -> Div {
        div()
            .block()
            .min_w(min_width)
            .max_w(max_width)
            .overflow_hidden()
            .child(tab)
    }
}

#[derive(IntoElement, RegisterComponent)]
pub struct TabBar {
    id: ElementId,
    start_children: SmallVec<[AnyElement; 2]>,
    children: SmallVec<[AnyElement; 2]>,
    end_children: SmallVec<[AnyElement; 2]>,
    scroll_handle: Option<ScrollHandle>,
}

impl TabBar {
    pub fn new(id: impl Into<ElementId>) -> Self {
        Self {
            id: id.into(),
            start_children: SmallVec::new(),
            children: SmallVec::new(),
            end_children: SmallVec::new(),
            scroll_handle: None,
        }
    }

    pub fn track_scroll(mut self, scroll_handle: &ScrollHandle) -> Self {
        self.scroll_handle = Some(scroll_handle.clone());
        self
    }

    pub fn start_children_mut(&mut self) -> &mut SmallVec<[AnyElement; 2]> {
        &mut self.start_children
    }

    pub fn start_child(mut self, start_child: impl IntoElement) -> Self
    where
        Self: Sized,
    {
        self.start_children_mut()
            .push(start_child.into_element().into_any());
        self
    }

    pub fn start_children(
        mut self,
        start_children: impl IntoIterator<Item = impl IntoElement>,
    ) -> Self
    where
        Self: Sized,
    {
        self.start_children_mut().extend(
            start_children
                .into_iter()
                .map(|child| child.into_any_element()),
        );
        self
    }

    pub fn end_children_mut(&mut self) -> &mut SmallVec<[AnyElement; 2]> {
        &mut self.end_children
    }

    pub fn end_child(mut self, end_child: impl IntoElement) -> Self
    where
        Self: Sized,
    {
        self.end_children_mut()
            .push(end_child.into_element().into_any());
        self
    }

    pub fn end_children(mut self, end_children: impl IntoIterator<Item = impl IntoElement>) -> Self
    where
        Self: Sized,
    {
        self.end_children_mut().extend(
            end_children
                .into_iter()
                .map(|child| child.into_any_element()),
        );
        self
    }
}

impl ParentElement for TabBar {
    fn extend(&mut self, elements: impl IntoIterator<Item = AnyElement>) {
        self.children.extend(elements)
    }
}

impl RenderOnce for TabBar {
    fn render(self, _: &mut Window, cx: &mut App) -> impl IntoElement {
        div()
            .id(self.id)
            .group("tab_bar")
            .flex()
            .flex_none()
            .w_full()
            .h(Tab::container_height(cx))
            .bg(cx.theme().colors().tab_bar_background)
            .when(!self.start_children.is_empty(), |this| {
                this.child(
                    h_flex()
                        .flex_none()
                        .gap(DynamicSpacing::Base04.rems(cx))
                        .px(DynamicSpacing::Base06.rems(cx))
                        .border_b_1()
                        .border_r_1()
                        .border_color(cx.theme().colors().border)
                        .children(self.start_children),
                )
            })
            .child(
                div()
                    .relative()
                    .flex_1()
                    .h_full()
                    .overflow_x_hidden()
                    .child(
                        div()
                            .absolute()
                            .top_0()
                            .left_0()
                            .size_full()
                            .border_b_1()
                            .border_color(cx.theme().colors().border),
                    )
                    .child(
                        h_flex()
                            .id("tabs")
                            .flex_grow_1()
                            .overflow_x_scroll()
                            .when_some(self.scroll_handle, |cx, scroll_handle| {
                                cx.track_scroll(&scroll_handle)
                            })
                            .children(self.children),
                    ),
            )
            .when(!self.end_children.is_empty(), |this| {
                this.child(
                    h_flex()
                        .flex_none()
                        .gap(DynamicSpacing::Base04.rems(cx))
                        .px(DynamicSpacing::Base06.rems(cx))
                        .border_color(cx.theme().colors().border)
                        .border_b_1()
                        .border_l_1()
                        .children(self.end_children),
                )
            })
    }
}

impl Component for TabBar {
    fn scope() -> ComponentScope {
        ComponentScope::Navigation
    }

    fn name() -> &'static str {
        "TabBar"
    }

    fn description() -> &'static str {
        "A horizontal bar containing tabs for navigation between different views \
        or sections."
    }

    fn preview(_window: &mut Window, _cx: &mut App) -> AnyElement {
        v_flex()
            .gap_6()
            .children(vec![
                example_group_with_title(
                    "Basic Usage",
                    vec![
                        single_example(
                            "Empty TabBar",
                            TabBar::new("empty_tab_bar").into_any_element(),
                        ),
                        single_example(
                            "With Tabs",
                            TabBar::new("tab_bar_with_tabs")
                                .child(Tab::new("tab1"))
                                .child(Tab::new("tab2"))
                                .child(Tab::new("tab3"))
                                .into_any_element(),
                        ),
                    ],
                ),
                example_group_with_title(
                    "With Start and End Children",
                    vec![single_example(
                        "Full TabBar",
                        TabBar::new("full_tab_bar")
                            .start_child(Button::new("start_button", "Start"))
                            .child(Tab::new("tab1"))
                            .child(Tab::new("tab2"))
                            .child(Tab::new("tab3"))
                            .end_child(Button::new("end_button", "End"))
                            .into_any_element(),
                    )],
                ),
            ])
            .into_any_element()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_tab_row_capacity_and_ranges() {
        // 1. tab_row_capacity with TAB_BAR_FILLER_MIN_WIDTH (24px)
        assert_eq!(
            tab_row_capacity(px(324.), px(100.), TAB_BAR_FILLER_MIN_WIDTH),
            3
        );
        assert_eq!(
            tab_row_capacity(px(323.), px(100.), TAB_BAR_FILLER_MIN_WIDTH),
            2
        );
        // Narrow width < min_width + filler clamps to at least 1
        assert_eq!(
            tab_row_capacity(px(50.), px(100.), TAB_BAR_FILLER_MIN_WIDTH),
            1
        );
        assert_eq!(
            tab_row_capacity(px(10.), px(100.), TAB_BAR_FILLER_MIN_WIDTH),
            1
        );
        // min_width <= 0 returns usize::MAX
        assert_eq!(
            tab_row_capacity(px(300.), px(0.), TAB_BAR_FILLER_MIN_WIDTH),
            usize::MAX
        );
        assert_eq!(
            tab_row_capacity(px(300.), px(-10.), TAB_BAR_FILLER_MIN_WIDTH),
            usize::MAX
        );

        // 2. tab_row_ranges
        // count == 0 -> [0..0]
        assert_eq!(tab_row_ranges(0, 2, 3, 3), vec![0..0]);
        // All tabs fit in row 0 -> [0..count]
        assert_eq!(tab_row_ranges(2, 3, 4, 3), vec![0..2]);
        // Exact capacity -> no extra empty row
        assert_eq!(tab_row_ranges(2, 2, 3, 3), vec![0..2]);
        assert_eq!(tab_row_ranges(5, 2, 3, 3), vec![0..2, 2..5]);
        // Overflow to 2 rows and 3 rows where first_row_capacity != row_capacity
        assert_eq!(tab_row_ranges(4, 2, 3, 3), vec![0..2, 2..4]);
        assert_eq!(tab_row_ranges(6, 2, 3, 3), vec![0..2, 2..5, 5..6]);
        // Remainder (> row_capacity) all placed in last row (max_rows - 1)
        assert_eq!(tab_row_ranges(11, 2, 3, 3), vec![0..2, 2..5, 5..11]);
        // max_rows == 1 or 0 -> [0..count]
        assert_eq!(tab_row_ranges(7, 2, 3, 1), vec![0..7]);
        assert_eq!(tab_row_ranges(7, 2, 3, 0), vec![0..7]);

        // 3. TabRows width tracking (1px threshold) & ranges guards
        let mut rows = TabRows::default();
        // Without measurements -> single row [0..count]
        assert_eq!(
            rows.ranges(6, Some(px(100.)), TAB_BAR_FILLER_MIN_WIDTH, 3),
            vec![0..6]
        );
        // Zero/negative widths ignored
        assert!(!rows.update_widths(Some(px(0.)), Some(px(0.))));
        // Initial measurement > 0 triggers true
        assert!(rows.update_widths(Some(px(224.)), Some(px(324.))));
        // Changes <= 1px ignored
        assert!(!rows.update_widths(Some(px(224.8)), Some(px(323.2))));
        assert_eq!(rows.first_row_width, Some(px(224.)));
        assert_eq!(rows.full_row_width, Some(px(324.)));
        // Changes > 1px trigger true and update stored width
        assert!(rows.update_widths(Some(px(226.)), Some(px(324.))));
        assert_eq!(rows.first_row_width, Some(px(226.)));

        // With first_row_width = 226px (cap 2 at min 100 + filler 24) and full_row_width = 324px (cap 3):
        assert_eq!(
            rows.ranges(7, Some(px(100.)), TAB_BAR_FILLER_MIN_WIDTH, 3),
            vec![0..2, 2..5, 5..7]
        );
        // max_rows <= 1 or min_width == None returns single range [0..count]
        assert_eq!(
            rows.ranges(7, Some(px(100.)), TAB_BAR_FILLER_MIN_WIDTH, 1),
            vec![0..7]
        );
        assert_eq!(
            rows.ranges(7, None, TAB_BAR_FILLER_MIN_WIDTH, 3),
            vec![0..7]
        );
    }
}
