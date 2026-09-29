#pragma once

#include "display-frame.h"

#include <cstdint>

namespace cube_content {

constexpr double kMotionMs = 900;
constexpr uint16_t kFullCoverage = 65535;

// Stateless spatial dissolve. The content layer owns targets and timing.
uint16_t DissolveDelay(uint16_t pixel);
struct DissolvePixelResult {
  cube_display::Rgb color;
  uint16_t coverage;
};
DissolvePixelResult DissolvePixel(cube_display::Rgb captured,
                                 uint16_t captured_coverage,
                                 cube_display::Rgb destination,
                                 uint16_t destination_coverage,
                                 double elapsed_ms, uint16_t delay_ms);

}  // namespace cube_content
