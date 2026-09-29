#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <stdexcept>

namespace cube_display {

constexpr int kWidth = 64;
constexpr int kHeight = 64;
constexpr std::size_t kPixels = kWidth * kHeight;

struct Rgb {
  uint8_t r = 0, g = 0, b = 0;
  bool operator==(const Rgb& other) const {
    return r == other.r && g == other.g && b == other.b;
  }
};

using Frame64 = std::array<Rgb, kPixels>;
using Mask64 = std::array<uint8_t, kPixels>;

constexpr std::size_t PixelIndex(int x, int y) {
  return static_cast<std::size_t>(y) * kWidth + x;
}

inline Rgb& PixelAt(Frame64& frame, int x, int y) {
  if (x < 0 || x >= kWidth || y < 0 || y >= kHeight)
    throw std::out_of_range("Frame64 pixel");
  return frame.at(PixelIndex(x, y));
}

inline const Rgb& PixelAt(const Frame64& frame, int x, int y) {
  if (x < 0 || x >= kWidth || y < 0 || y >= kHeight)
    throw std::out_of_range("Frame64 pixel");
  return frame.at(PixelIndex(x, y));
}

}  // namespace cube_display
