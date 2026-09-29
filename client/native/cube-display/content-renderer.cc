#include "content-renderer.h"

#include <algorithm>
#include <cmath>

namespace cube_content {
namespace {
constexpr Rgb kContentWhite{255, 255, 255};
const char* StateName(Presentation state) {
  switch (state) {
    case Presentation::Full: return "full";
    case Presentation::FormingContent: return "forming_content";
    case Presentation::Content: return "content";
    case Presentation::MorphingContent: return "morphing_content";
    case Presentation::ReleasingContent: return "releasing_content";
  }
  return "full";
}
}  // namespace

ContentRenderer::ContentRenderer() {
  target_.fill(1);
  for (uint16_t i = 0; i < kPixels; ++i) dissolve_delays_[i] = DissolveDelay(i);
}

bool ContentRenderer::RequestMask(const Mask64& mask) {
  if (!ValidMask(mask)) return false;
  pending_mask_ = mask;
  pending_full_ = false;
  pending_ = true;
  return true;
}

void ContentRenderer::ClearContent() {
  pending_mask_.fill(1);
  pending_full_ = true;
  pending_ = true;
}

bool ContentRenderer::ApplyPending(bool visible) {
  if (!pending_) return false;
  pending_ = false;
  if (target_full_ == pending_full_ && target_ == pending_mask_) return false;
  const bool from_full = state_ == Presentation::Full;
  target_ = pending_mask_;
  target_full_ = pending_full_;
  motion_ms_ = 0;
  if (!visible || !have_presented_ || !last_visible_ ||
      CountPixels(presented_occupancy_) == 0) {
    state_ = target_full_ ? Presentation::Full : Presentation::Content;
    fresh_ = false;
    return true;
  }
  transition_frame_ = presented_;
  transition_coverage_ = presented_coverage_;
  state_ = target_full_ ? Presentation::ReleasingContent :
      (from_full ? Presentation::FormingContent : Presentation::MorphingContent);
  fresh_ = true;
  return true;
}

bool ContentRenderer::transitioning() const {
  return state_ != Presentation::Full && state_ != Presentation::Content;
}

void ContentRenderer::DrawDissolve(const Frame64& live) {
  for (std::size_t i = 0; i < kPixels; ++i) {
    // Black base pixels carry no visible material. Treat them as zero coverage
    // so white text visibly follows the same spatial dissolve on entry/exit.
    const uint16_t from = transition_frame_[i] == Rgb{} ? 0 : transition_coverage_[i];
    const uint16_t to = target_[i] && (!target_full_ || !(live[i] == Rgb{}))
        ? kFullCoverage : 0;
    const Rgb current = target_full_ && !(live[i] == Rgb{}) ? live[i] : kContentWhite;
    const auto result = DissolvePixel(transition_frame_[i], from, current, to,
                                      motion_ms_, dissolve_delays_[i]);
    frame_[i] = result.color;
    coverage_[i] = result.coverage;
    occupancy_[i] = result.coverage != 0;
  }
}

void ContentRenderer::DrawMasked(const Frame64& live) {
  occupancy_ = target_;
  for (std::size_t i = 0; i < kPixels; ++i) {
    frame_[i] = target_[i] ? (target_full_ ? live[i] : kContentWhite) : Rgb{};
    coverage_[i] = target_[i] ? kFullCoverage : 0;
  }
}

const Frame64& ContentRenderer::Compose(const Frame64& live, double elapsed_ms) {
  const double step = std::isfinite(elapsed_ms) ? std::max(0.0, elapsed_ms) : 0;
  if (!transitioning()) {
    DrawMasked(live);
    return frame_;
  }
  if (fresh_) {
    // Hold the exact snapshot until submission; later compositions use live
    // colors even when the caller keeps motion time fixed with a zero dt.
    frame_ = transition_frame_;
    coverage_ = transition_coverage_;
    occupancy_ = presented_occupancy_;
    return frame_;
  }
  motion_ms_ = std::min(kMotionMs, motion_ms_ + step);
  if (motion_ms_ == kMotionMs) {
    // Finish at the exact mask and current live colors.
    DrawMasked(live);
    state_ = target_full_ ? Presentation::Full : Presentation::Content;
  } else DrawDissolve(live);
  return frame_;
}

void ContentRenderer::Presented(bool visible) {
  have_presented_ = true;
  last_visible_ = visible;
  if (visible) {
    presented_ = frame_;
    presented_occupancy_ = occupancy_;
    presented_coverage_ = coverage_;
  } else {
    presented_.fill({});
    presented_occupancy_.fill(0);
    presented_coverage_.fill(0);
  }
  // Logical animation can continue behind display/master blanking.
  fresh_ = false;
}

double ContentRenderer::progress() const {
  if (!transitioning()) return 1;
  return motion_ms_ / kMotionMs;
}

std::string ContentRenderer::Status() const {
  return std::string("{\"available\":true,\"presentation\":\"") + StateName(state_) +
      "\",\"target_pixels\":" + std::to_string(CountPixels(target_)) +
      ",\"progress\":" + std::to_string(progress()) +
      ",\"pending\":" + (pending_ ? "true}" : "false}");
}

}  // namespace cube_content
