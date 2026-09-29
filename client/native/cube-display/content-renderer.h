#pragma once

#include "content-mask.h"
#include "dissolve-transition.h"

#include <array>
#include <cstdint>
#include <string>

namespace cube_content {

using cube_display::Rgb;
using cube_display::Frame64;
using Coverage64 = std::array<uint16_t, kPixels>;

enum class Presentation { Full, FormingContent, Content, MorphingContent, ReleasingContent };
// No clocks, matrix APIs, business meanings, or per-frame allocations. The
// caller supplies animation elapsed time and confirms actual frame submission.
class ContentRenderer {
 public:
  ContentRenderer();
  bool RequestMask(const Mask64& mask);
  void ClearContent();
  // Call once after the socket batch and final display controls. Returns true
  // when a distinct target was applied (for optional timing diagnostics).
  bool ApplyPending(bool visible);
  const Frame64& Compose(const Frame64& live, double elapsed_ms);
  // Call only after SwapOnVSync. Blank output must not become new material.
  void Presented(bool visible);

  Presentation state() const { return state_; }
  const Mask64& occupancy() const { return occupancy_; }
  const Mask64& presented_occupancy() const { return presented_occupancy_; }
  const Coverage64& coverage() const { return coverage_; }
  const Coverage64& presented_coverage() const { return presented_coverage_; }
  const Frame64& presented_frame() const { return presented_; }
  bool pending() const { return pending_; }
  double progress() const;
  std::string Status() const;

 private:
  void DrawDissolve(const Frame64& live);
  void DrawMasked(const Frame64& live);
  bool transitioning() const;

  Presentation state_ = Presentation::Full;
  Mask64 target_{}, pending_mask_{}, occupancy_{}, presented_occupancy_{};
  Frame64 frame_{}, presented_{}, transition_frame_{};
  Coverage64 coverage_{}, presented_coverage_{}, transition_coverage_{};
  std::array<uint16_t, kPixels> dissolve_delays_{};
  bool pending_ = false, pending_full_ = false, target_full_ = true;
  bool fresh_ = false;
  bool have_presented_ = false, last_visible_ = false;
  double motion_ms_ = 0;
};

}  // namespace cube_content
