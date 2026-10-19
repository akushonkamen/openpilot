"""
Tests for child -> parent gesture bubbling between a child widget and GuiScrollPanel2.

A child widget (e.g. the mici bookmark swipe on the onroad view) must be able to
reliably claim a leftward drag so the parent scroller never scrolls, even when a
fast press+drag arrives within a single frame batch. See commaai/openpilot#38934.

The BookmarkIcon.is_gesture_claimed() logic (leftward drags inside the child's hit
rect, evaluated live on the current frame's raw events) is mirrored in
_BbookmarkClaimProbe below; augmented_road_view itself cannot be imported headless
(it pulls in msgq/visionipc). The probe drives a real GuiScrollPanel2 the same way
the Scroller does, with set_enabled(lambda: not claimed).

The application module is stubbed because it opens a window on import; GuiScrollPanel2
only needs gui_app (for event injection) and MouseEvent from it.
"""
import sys
import time
import unittest
from types import ModuleType
from typing import NamedTuple

import pyray as rl

BOUNDS = rl.Rectangle(0, 0, 1000, 500)


class MousePos(NamedTuple):
  x: float
  y: float


class MouseEvent(NamedTuple):
  pos: MousePos
  slot: int
  left_pressed: bool
  left_released: bool
  left_down: bool
  t: float


class FakeGuiApp:
  def __init__(self):
    self.mouse_events: list[MouseEvent] = []


def _install_fake_application_module():
  mod = ModuleType("openpilot.system.ui.lib.application")
  mod.MousePos = MousePos
  mod.MouseEvent = MouseEvent
  mod.gui_app = FakeGuiApp()
  sys.modules["openpilot.system.ui.lib.application"] = mod
  return mod


_fake_app = _install_fake_application_module()
from openpilot.system.ui.lib.scroll_panel2 import GuiScrollPanel2, MIN_DRAG_PIXELS, ScrollState


class _BookmarkClaimProbe:
  """Mirrors BookmarkIcon.is_gesture_claimed() from mici/onroad/augmented_road_view.py."""

  def __init__(self, hit_rect: rl.Rectangle, started: bool = True):
    self._hit_rect = hit_rect
    self._started = started
    self.is_swiping = False
    self.is_swiping_left = False

  def is_gesture_claimed(self, mouse_events: list[MouseEvent]) -> bool:
    if not self._started:
      return False
    if self.is_swiping and self.is_swiping_left:
      return True
    press_x = None
    for mouse_event in mouse_events:
      if mouse_event.left_pressed and rl.check_collision_point_rec(mouse_event.pos, self._hit_rect):
        press_x = mouse_event.pos.x
      elif press_x is not None and mouse_event.left_down and mouse_event.pos.x < press_x:
        return True
    return False


class TestGestureBubbling(unittest.TestCase):
  def setUp(self):
    self.gui_app = _fake_app.gui_app
    self.gui_app.mouse_events = []
    self.panel = GuiScrollPanel2(horizontal=True)
    # child owns the onroad view area of the scroller
    self.child = _BookmarkClaimProbe(rl.Rectangle(0, 0, 1000, 500))
    self.panel.set_enabled(lambda: not self.child.is_gesture_claimed(self.gui_app.mouse_events))
    self.now = time.monotonic()
    self.scroll_offset = 0.0

  def _event(self, x: float, pressed=False, released=False) -> MouseEvent:
    self.now += 1 / 140
    return MouseEvent(MousePos(x, 250), 0, pressed, released, not released, self.now)

  def _feed(self, *events: MouseEvent):
    self.gui_app.mouse_events = list(events)
    self.panel.update(BOUNDS, 2000)  # content larger than bounds
    self.gui_app.mouse_events = []

  def _swipe(self, start_x: float, end_x: float, batch: bool):
    """Swipe as a series of events, either all in one frame batch or across frames."""
    steps = 8
    xs = [start_x + (end_x - start_x) * i / steps for i in range(steps + 1)]
    if batch:
      events = [self._event(xs[0], pressed=True)] + [self._event(x) for x in xs[1:]]
      self._feed(*events)
      self._simulate_child_event_processing(start_x, end_x)
    else:
      self._feed(self._event(xs[0], pressed=True))
      for x in xs[1:]:
        self._feed(self._event(x))
        # the child processes the same frame's events after the scroller and learns
        # the swipe direction from them
        self._simulate_child_event_processing(start_x, x)

  def _simulate_child_event_processing(self, start_x: float, end_x: float):
    """Mirror what BookmarkIcon._handle_mouse_event learns from a full swipe."""
    self.child.is_swiping = True
    self.child.is_swiping_left = end_x < start_x

  def test_left_swipe_across_frames_does_not_scroll(self):
    """The reported bug: leftward bookmark swipe must not move the onroad view."""
    self._swipe(500, 500 - 4 * MIN_DRAG_PIXELS, batch=False)
    self.assertEqual(self.panel.get_offset(), 0.0, "leftward bookmark swipe must not scroll the parent")
    self.assertEqual(self.panel.state, ScrollState.STEADY)

  def test_left_swipe_single_fast_batch_does_not_scroll(self):
    """Press + drag past the scroller threshold in one frame batch must not scroll."""
    self._swipe(500, 500 - 4 * MIN_DRAG_PIXELS, batch=True)
    self.assertEqual(self.panel.get_offset(), 0.0, "same-frame leftward drag must not scroll the parent")
    self.assertEqual(self.panel.state, ScrollState.STEADY)

  def test_right_swipe_scrolls_normally(self):
    """Rightward swipes are not claimed: the parent scroller must still work."""
    self._swipe(500, 500 + 2 * MIN_DRAG_PIXELS, batch=True)
    self.assertGreater(self.panel.get_offset(), 0.0, "rightward swipe must still scroll the parent")
    self.assertEqual(self.panel.state, ScrollState.MANUAL_SCROLL)

  def test_slow_right_swipe_scrolls_normally(self):
    self._swipe(500, 500 + 2 * MIN_DRAG_PIXELS, batch=False)
    self.assertGreater(self.panel.get_offset(), 0.0, "rightward swipe must still scroll the parent")

  def test_click_does_not_scroll_or_block(self):
    """A plain press+release inside the child must leave the scroller usable."""
    self._feed(self._event(500, pressed=True))
    self._feed(self._event(500, released=True))
    self.assertEqual(self.panel.state, ScrollState.STEADY)
    self.assertEqual(self.panel.get_offset(), 0.0)
    self.assertFalse(self.child.is_gesture_claimed([]))

  def test_child_not_started_does_not_claim(self):
    """Offroad (not started) the child must not claim, so scrolling keeps working."""
    self.child = _BookmarkClaimProbe(rl.Rectangle(0, 0, 1000, 500), started=False)
    self.panel.set_enabled(lambda: not self.child.is_gesture_claimed(self.gui_app.mouse_events))
    self._swipe(500, 500 - 4 * MIN_DRAG_PIXELS, batch=True)
    self.assertLess(self.panel.get_offset(), 0.0, "offroad leftward swipe must scroll normally")

  def test_press_outside_child_rect_scrolls(self):
    """Presses outside the child's hit rect are never claimed."""
    self.child = _BookmarkClaimProbe(rl.Rectangle(0, 0, 400, 500))
    self.panel.set_enabled(lambda: not self.child.is_gesture_claimed(self.gui_app.mouse_events))
    self._feed(self._event(700, pressed=True), self._event(700 - MIN_DRAG_PIXELS - 5),
               self._event(700 - 2 * MIN_DRAG_PIXELS - 5))
    self.assertEqual(self.panel.state, ScrollState.MANUAL_SCROLL)
    self.assertLess(self.panel.get_offset(), 0.0)


if __name__ == "__main__":
  unittest.main()
