#pragma once

#include "display-frame.h"

#include <algorithm>
#include <cmath>

namespace cube_display {

enum class CubeState { Idle, Listening, Thinking, Speaking, Followup };

// A value supplied by the existing interaction owner; this has no lifecycle.
struct PresentationState {
  const CubeState state;
  const double speech_level;
  const double state_elapsed_seconds = 0;
};

class CubeStateOverlay {
 public:
  // Only the bottom row is touched. All effects depend solely on the immutable
  // snapshot; the interaction owner and app know nothing about these pixels.
  static void Apply(const PresentationState& state, Frame64* frame) {
    constexpr uint8_t kLineGray = 112;
    constexpr uint8_t kHighlightWhite = 255;
    constexpr double kRevealSeconds = 0.65;
    constexpr double kThinkingCycleSeconds = 1.6;
    constexpr double kHighlightRadius = 7;
    const double elapsed = std::isfinite(state.state_elapsed_seconds)
        ? std::max(0.0, state.state_elapsed_seconds) : 0.0;
    if (state.state == CubeState::Idle) return;

    uint8_t steady_white = kLineGray;
    if (state.state == CubeState::Speaking) {
      const double level = std::isfinite(state.speech_level)
          ? std::clamp(state.speech_level, 0.0, 1.0) : 0.0;
      steady_white = static_cast<uint8_t>(
          kLineGray + std::lround(level * (kHighlightWhite - kLineGray)));
    }
    const double reveal = std::clamp(elapsed / kRevealSeconds, 0.0, 1.0);
    const double remaining = 1.0 - reveal;
    const double extent = (1.0 - remaining * remaining * remaining) * kWidth / 2.0;
    const double reveal_head = std::floor(extent - 0.5) + 0.5;
    const double wave_center = -kHighlightRadius +
        std::fmod(elapsed, kThinkingCycleSeconds) / kThinkingCycleSeconds *
        (kWidth + 2 * kHighlightRadius);
    const int wave_peak = static_cast<int>(std::lround(wave_center));
    for (int x = 0; x < kWidth; ++x) {
      uint8_t white = steady_white;
      if (state.state == CubeState::Listening) {
        const double distance = std::abs(x - (kWidth - 1) / 2.0);
        if (distance > extent) continue;
        if (reveal < 1.0) {
          const double crest = std::max(0.0, 1.0 - std::abs(distance - reveal_head) / 4.0);
          white = static_cast<uint8_t>(
              kLineGray + std::lround((kHighlightWhite - kLineGray) * crest));
        }
      } else if (state.state == CubeState::Thinking) {
        const double crest = std::max(0.0, 1.0 - std::abs(x - wave_peak) / kHighlightRadius);
        white = static_cast<uint8_t>(
            kLineGray + std::lround((kHighlightWhite - kLineGray) * crest));
      }
      PixelAt(*frame, x, kHeight - 1) = {white, white, white};
    }
  }
};

inline Frame64 Compose(const Frame64& base, const PresentationState& state) {
  Frame64 result = base;
  CubeStateOverlay::Apply(state, &result);
  return result;
}

}  // namespace cube_display
