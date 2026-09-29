#include "carousel-scheduler.h"
#include "content-renderer.h"
#include "display-compositor.h"
#include "display-control.h"

#include <cassert>
#include <chrono>
#include <iostream>
#include <string>

using namespace std::chrono_literals;
using namespace cube_display;
using Duration = std::chrono::steady_clock::duration;

namespace {
DisplayContext Context(Duration now) {
  // Deliberately unrelated frame elapsed: dwell must use monotonic now.
  return {std::chrono::steady_clock::time_point{} + now, 33ms};
}

class SolidApp final : public DisplayApp {
 public:
  explicit SolidApp(Rgb color) : color_(color) {}
  bool available = true;
  int renders = 0;
  bool Available(const DisplayContext&) const override { return available; }
  void Render(const DisplayContext&, Frame64* frame) override {
    ++renders;
    frame->fill(color_);
  }
 private:
  Rgb color_;
};

struct Fixture {
  SolidApp a{{10, 20, 30}}, b{{40, 50, 60}}, c{{70, 80, 90}};
  DisplayAppRegistry registry;
  CarouselScheduler scheduler{registry};
  DisplayRuntime runtime;
  DisplayApp* installed = nullptr;
  int changes = 0;

  Fixture() {
    // Registration order intentionally differs from schedule order.
    assert(registry.Register("c", c));
    assert(registry.Register("a", a));
    assert(registry.Register("b", b));
    assert(scheduler.Configure({{"a", 15s}, {"b", 15s}, {"c", 10s}}));
  }

  DisplayApp* Tick(Duration now, bool paused = false) {
    auto* selected = scheduler.Update(Context(now), paused);
    if (selected != installed) {
      runtime.SetActiveApp(selected);
      installed = selected;
      ++changes;
    }
    return selected;
  }
};

void Registry() {
  SolidApp a{{1, 2, 3}}, b{{4, 5, 6}};
  {
    DisplayAppRegistry registry;
    assert(!registry.Contains("a") && registry.Find("missing") == nullptr);
    std::string id = "a";
    assert(registry.Register(id, a));
    id = "changed";
    assert(registry.Contains("a") && registry.Find("a") == &a);
    assert(!registry.Contains("A"));
    assert(!registry.Register("a", b));
    assert(!registry.Register("", b));
    assert(registry.Find("a") == &a);
    for (int i = 0; i < 100; ++i)
      assert(registry.Register("extra-" + std::to_string(i), b));
    assert(registry.Find("a") == &a);
    a.available = false;
    assert(!registry.Find("a")->Available(Context(0s)));
  }
  // Destroying the registry does not destroy or move its caller-owned apps.
  a.available = true;
  Frame64 frame{};
  a.Render(Context(0s), &frame);
  assert(frame.front() == (Rgb{1, 2, 3}));
}

void RotationAndBoundaries() {
  Fixture f;
  assert(f.Tick(0s) == &f.a);
  assert(f.Tick(15s - 1ns) == &f.a);
  assert(f.Tick(15s) == &f.b);
  assert(f.Tick(15s + 1ns) == &f.b);
  assert(f.Tick(30s) == &f.c);
  assert(f.Tick(40s) == &f.a);
  assert(f.changes == 4);

  Fixture after;
  assert(after.Tick(0s) == &after.a);
  assert(after.Tick(15s + 1ns) == &after.b);
  assert(after.Tick(30s) == &after.b);  // New dwell starts at actual selection.
  assert(after.Tick(30s + 1ns) == &after.c);

  Fixture different;
  assert(different.scheduler.Configure({{"a", 5s}, {"b", 15s}, {"c", 3s}}));
  assert(different.Tick(0s) == &different.a);
  assert(different.Tick(5s - 1ns) == &different.a);
  assert(different.Tick(5s) == &different.b);
  assert(different.Tick(20s - 1ns) == &different.b);
  assert(different.Tick(20s) == &different.c);
  assert(different.Tick(23s) == &different.a);
}

void Availability() {
  Fixture f;
  f.b.available = false;
  assert(f.Tick(0s) == &f.a);
  assert(f.Tick(15s) == &f.c);
  assert(f.Tick(25s) == &f.a);
  f.a.available = false;
  assert(f.Tick(26s) == &f.c);  // Immediate loss, well before A's expiry.
  f.b.available = true;
  assert(f.Tick(27s) == &f.c);  // Recovery must not interrupt C.
  assert(f.Tick(36s - 1ns) == &f.c);
  assert(f.Tick(36s) == &f.b);  // Wrap past unavailable A.
  f.b.available = false;
  f.c.available = false;
  assert(f.Tick(37s) == nullptr);
  assert(f.runtime.Render(Context(37s)) == Frame64{});
  assert(f.Tick(100s) == nullptr);
  f.a.available = f.c.available = true;
  assert(f.Tick(101s) == &f.a);  // No selection recovers in configured order.
  assert(f.Tick(116s) == &f.c);

  Fixture none;
  none.a.available = none.b.available = none.c.available = false;
  assert(none.Tick(0s) == nullptr && none.changes == 0);
  none.b.available = true;
  assert(none.Tick(1s) == &none.b);
}

void SingleAndEmpty() {
  Fixture f;
  f.b.available = f.c.available = false;
  assert(f.Tick(0s) == &f.a);
  for (int i = 1; i <= 100; ++i) {
    const auto now = i * 15s;
    assert(f.Tick(now) == &f.a);
    assert(f.runtime.Render(Context(now)).front() == (Rgb{10, 20, 30}));
  }
  assert(f.changes == 1 && f.a.renders == 100);
  assert(f.scheduler.Configure({}));
  assert(f.Tick(2000s) == nullptr);
  assert(f.runtime.Render(Context(2000s)) == Frame64{});
  assert(f.Tick(3000s) == nullptr && f.changes == 2);

  DisplayAppRegistry registry;
  CarouselScheduler empty(registry);
  assert(empty.Update(Context(0s), false) == nullptr);
  assert(empty.Configure({}));
  assert(empty.Update(Context(10s), true) == nullptr);
}

void ValidationAndReplacement() {
  Fixture f;
  assert(f.Tick(0s) == &f.a);
  assert(f.Tick(6s) == &f.a);
  std::string error;
  for (const auto duration : {Duration::zero(), Duration(-1)}) {
    assert(!f.scheduler.Configure({{"a", duration}}, &error));
    assert(error.find("nonpositive duration") != std::string::npos);
  }
  assert(!f.scheduler.Configure({{"a", 1s}, {"missing", 1s}}, &error));
  assert(error.find("unknown app ID: missing") != std::string::npos);
  assert(!f.scheduler.Configure({{"a", 1s}, {"a", 2s}}, &error));
  assert(error.find("duplicate app ID") != std::string::npos);
  assert(!f.scheduler.Configure({{"", 1s}}, &error));
  assert(error.find("empty app ID") != std::string::npos);
  assert(f.Tick(15s - 1ns) == &f.a);
  assert(f.Tick(15s) == &f.b);  // Failed configurations preserved original dwell.

  assert(f.scheduler.Configure({{"c", 3s}, {"a", 5s}}, &error));
  assert(error.empty());
  assert(f.Tick(100s) == &f.c);
  assert(f.Tick(103s) == &f.a);
  assert(f.Tick(108s) == &f.c);  // Omitted B never participates.
  assert(f.scheduler.Configure({{"c", 10s}}));
  const auto changes = f.changes;
  assert(f.Tick(200s) == &f.c && f.changes == changes);
  assert(f.Tick(209s) == &f.c);
}

void PauseAndJumps() {
  Fixture f;
  assert(f.Tick(0s) == &f.a);
  assert(f.Tick(6s, true) == &f.a);
  assert(f.Tick(7s, true) == &f.a);
  assert(f.Tick(8s) == &f.a);
  assert(f.Tick(17s - 1ns) == &f.a);
  assert(f.Tick(17s) == &f.b);  // Nine visible seconds after resume.
  assert(f.Tick(18s, true) == &f.b);
  assert(f.Tick(24h, true) == &f.b);
  assert(f.Tick(48h) == &f.b);
  assert(f.Tick(48h + 14s - 1ns) == &f.b);
  assert(f.Tick(48h + 14s) == &f.c);

  Fixture jump;
  assert(jump.Tick(0s) == &jump.a);
  assert(jump.Tick(24h) == &jump.b);
  for (int i = 0; i < 10; ++i) assert(jump.Tick(24h) == &jump.b);
  assert(jump.Tick(24h + 15s - 1ns) == &jump.b);
  assert(jump.Tick(24h + 15s) == &jump.c);
  assert(jump.changes == 3);

  Fixture boundary;
  assert(boundary.Tick(0s) == &boundary.a);
  assert(boundary.Tick(15s, true) == &boundary.a);
  assert(boundary.Tick(1h, true) == &boundary.a);
  assert(boundary.Tick(2h) == &boundary.b);  // Expired dwell waits until resume.
}

void AvailabilityWhilePaused() {
  Fixture f;
  assert(f.Tick(0s, true) == &f.a);
  f.a.available = false;
  assert(f.Tick(1s, true) == &f.b);
  f.a.available = true;
  assert(f.Tick(2s, true) == &f.b);
  assert(f.Tick(1h) == &f.b);
  assert(f.Tick(1h + 15s) == &f.c);  // B received a fresh, frozen full dwell.
  f.a.available = f.b.available = f.c.available = false;
  assert(f.Tick(1h + 16s, true) == nullptr);
  f.a.available = true;
  assert(f.Tick(2h, true) == &f.a);
  assert(f.Tick(3h) == &f.a);
  f.b.available = true;
  assert(f.Tick(3h + 15s) == &f.b);
}

void ContentIntegration() {
  Fixture f;
  cube_content::ContentRenderer content;
  DisplayControl control;
  Duration previous{};
  auto tick = [&](Duration now, CubeState state = CubeState::Idle) {
    content.ApplyPending(control.visible());
    f.Tick(now, content.state() != cube_content::Presentation::Full);
    const auto base = f.runtime.Render(Context(now));
    const auto composed = Compose(base, {state, 0.5, 1.0});
    const Frame64 result = content.Compose(
        composed, std::chrono::duration<double, std::milli>(now - previous).count());
    content.Presented(control.visible());
    previous = now;
    return result;
  };
  assert(tick(0s).front() == (Rgb{10, 20, 30}));
  cube_content::Mask64 mask{};
  assert(cube_content::TextMask("50%", &mask));
  assert(content.RequestMask(mask));
  tick(6s);
  assert(content.state() == cube_content::Presentation::FormingContent);
  const auto white = tick(6900ms, CubeState::Thinking);
  assert(content.state() == cube_content::Presentation::Content);
  for (std::size_t i = 0; i < kPixels; ++i)
    assert(white[i] == (mask[i] ? Rgb{255, 255, 255} : Rgb{}));
  assert(cube_content::TextMask("MUTE", &mask));
  assert(content.RequestMask(mask));
  tick(7s);
  assert(content.state() == cube_content::Presentation::MorphingContent);
  tick(7900ms);
  assert(content.state() == cube_content::Presentation::Content);
  content.ClearContent();
  tick(8s);
  assert(content.state() == cube_content::Presentation::ReleasingContent);
  tick(8900ms);
  assert(content.state() == cube_content::Presentation::Full);
  tick(9s);  // First tick observing full presentation resumes nine remaining seconds.
  assert(f.installed == &f.a && f.changes == 1);
  const auto overlay = tick(17999ms, CubeState::Listening);
  assert(overlay.front() == (Rgb{10, 20, 30}));
  assert(!(overlay.back() == overlay.front()));
  assert(tick(18s).front() == (Rgb{40, 50, 60}));  // Direct switch, no app dissolve.
  assert(f.installed == &f.b);
  assert(control.Apply("control set_display off"));
  assert(control.Apply("control set_brightness 0"));
  tick(33s);
  assert(f.installed == &f.c);  // Output blanking does not stop logical dwell.
  assert(!control.visible() && control.master_brightness_percent == 0);
}
}  // namespace

int main() {
  Registry();
  RotationAndBoundaries();
  Availability();
  SingleAndEmpty();
  ValidationAndReplacement();
  PauseAndJumps();
  AvailabilityWhilePaused();
  ContentIntegration();
  std::cout << "Native carousel assertions passed\n";
}
