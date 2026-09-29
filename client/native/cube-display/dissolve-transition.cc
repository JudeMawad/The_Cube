#include "dissolve-transition.h"

#include <algorithm>
#include <cmath>

namespace cube_content {
namespace {
float Smooth(float value) {
  const float t = std::clamp(value, 0.0f, 1.0f);
  return t * t * (3.0f - 2.0f * t);
}
uint32_t Hash(uint32_t value) {
  value ^= value >> 16;
  value *= 0x7feb352dU;
  value ^= value >> 15;
  value *= 0x846ca68bU;
  return value ^ (value >> 16);
}
float UnitHash(uint32_t value) {
  return static_cast<float>(Hash(value) & 0xffffU) / 65535.0f;
}
}  // namespace

uint16_t DissolveDelay(uint16_t pixel) {
  const int x = pixel % cube_display::kWidth, y = pixel / cube_display::kWidth;
  const int gx = x / 8, gy = y / 8;
  const float tx = Smooth((x % 8) / 8.0f), ty = Smooth((y % 8) / 8.0f);
  const auto sample = [](int px, int py) {
    return UnitHash(0x44495353U ^ static_cast<uint32_t>(py * 9 + px));
  };
  const float top = sample(gx, gy) * (1 - tx) + sample(gx + 1, gy) * tx;
  const float bottom = sample(gx, gy + 1) * (1 - tx) + sample(gx + 1, gy + 1) * tx;
  return static_cast<uint16_t>(std::lround(450 * (top * (1 - ty) + bottom * ty)));
}

DissolvePixelResult DissolvePixel(cube_display::Rgb captured,
                                 uint16_t captured_coverage,
                                 cube_display::Rgb destination,
                                 uint16_t destination_coverage,
                                 double elapsed_ms, uint16_t delay_ms) {
  const int delay = destination_coverage > captured_coverage ? 450 - delay_ms : delay_ms;
  const float blend = Smooth(static_cast<float>((elapsed_ms - delay) / 450.0));
  const auto coverage = static_cast<uint16_t>(std::lround(
      captured_coverage + (int(destination_coverage) - captured_coverage) * blend));
  const float opacity = coverage / static_cast<float>(kFullCoverage);
  const float handoff = Smooth(static_cast<float>(elapsed_ms / 120.0));
  const auto channel = [&](uint8_t prior, uint8_t current) {
    // The captured RGB already contains coverage. Recover color first so an
    // interrupted transition never applies opacity twice.
    const float source = captured_coverage
        ? std::min(255.0f, prior * float(kFullCoverage) / captured_coverage) : current;
    const float value = handoff >= 1 ? current : source + (current - source) * handoff;
    return static_cast<uint8_t>(std::lround(value * opacity));
  };
  return {{channel(captured.r, destination.r), channel(captured.g, destination.g),
           channel(captured.b, destination.b)}, coverage};
}

}  // namespace cube_content
