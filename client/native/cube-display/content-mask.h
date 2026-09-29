#pragma once

#include "display-frame.h"
#include <string_view>

namespace cube_content {

constexpr int kSide = cube_display::kWidth;
using cube_display::kPixels;
using cube_display::Mask64;

// Producers leave output untouched on failure. A mask is binary and nonempty;
// FULL/clear is a separate presentation operation, never an empty mask.
bool ValidMask(const Mask64& mask);
std::size_t CountPixels(const Mask64& mask);
bool TextMask(std::string_view text, Mask64* output);
bool IconMask(std::string_view name, Mask64* output);

}  // namespace cube_content
