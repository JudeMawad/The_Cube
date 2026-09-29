#pragma once

#include "display-frame.h"

#include <array>
#include <string_view>

namespace cube_display::pixel_font {

constexpr int kWidth = 5;
constexpr int kHeight = 9;
constexpr int kAdvance = 6;

// Hand-drawn 5x9 bold numerals and the uppercase subset used by English dates.
// Most main strokes are two pixels thick, with open counters for reverse text.
// Row bits run left to right. Unsupported characters are blank.
constexpr std::array<uint8_t, kHeight> Glyph(char c) {
  switch (c) {
    case '0': return {14, 31, 27, 27, 27, 27, 27, 31, 14};
    case '1': return {6, 14, 6, 6, 6, 6, 6, 31, 31};
    case '2': return {31, 31, 3, 3, 31, 31, 24, 24, 31};
    case '3': return {31, 31, 3, 3, 15, 15, 3, 3, 31};
    case '4': return {6, 14, 22, 27, 31, 31, 3, 3, 3};
    case '5': return {31, 31, 24, 24, 31, 31, 3, 3, 31};
    case '6': return {31, 31, 24, 24, 31, 31, 27, 27, 31};
    case '7': return {31, 31, 3, 3, 6, 6, 12, 12, 12};
    case '8': return {31, 31, 27, 27, 31, 27, 27, 31, 31};
    case '9': return {31, 31, 27, 27, 31, 31, 3, 3, 31};
    case '%': return {17, 19, 2, 4, 4, 8, 25, 17, 0};
    case 'A': return {14, 31, 27, 27, 31, 31, 27, 27, 27};
    case 'B': return {30, 31, 27, 27, 30, 27, 27, 31, 30};
    case 'C': return {15, 31, 24, 24, 24, 24, 24, 31, 15};
    case 'D': return {30, 31, 27, 27, 27, 27, 27, 31, 30};
    case 'E': return {31, 31, 24, 24, 30, 30, 24, 24, 31};
    case 'F': return {31, 31, 24, 24, 30, 30, 24, 24, 24};
    case 'G': return {15, 31, 24, 24, 27, 27, 27, 31, 15};
    case 'H': return {27, 27, 27, 27, 31, 27, 27, 27, 27};
    case 'I': return {31, 31, 6, 6, 6, 6, 6, 31, 31};
    case 'J': return {7, 7, 3, 3, 3, 27, 27, 31, 14};
    case 'L': return {24, 24, 24, 24, 24, 24, 24, 31, 31};
    case 'M': return {17, 27, 31, 21, 21, 27, 27, 27, 27};
    case 'N': return {27, 31, 31, 31, 27, 27, 27, 27, 27};
    case 'O': return {14, 31, 27, 27, 27, 27, 27, 31, 14};
    case 'P': return {30, 31, 27, 27, 30, 30, 24, 24, 24};
    case 'R': return {30, 31, 27, 27, 30, 30, 28, 26, 27};
    case 'S': return {15, 31, 24, 24, 14, 14, 3, 3, 30};
    case 'T': return {31, 31, 6, 6, 6, 6, 6, 6, 6};
    case 'U': return {27, 27, 27, 27, 27, 27, 27, 31, 14};
    case 'V': return {27, 27, 27, 27, 27, 27, 27, 14, 4};
    case 'W': return {27, 27, 27, 27, 27, 31, 31, 27, 27};
    case 'Y': return {27, 27, 27, 14, 6, 6, 6, 6, 6};
    default: return {};
  }
}

inline void Draw(Frame64& frame, std::string_view text, int left, int top, Rgb color) {
  for (char c : text) {
    const auto rows = Glyph(c);
    for (int y = 0; y < kHeight; ++y)
      for (int x = 0; x < kWidth; ++x)
        if ((rows[y] & (16 >> x)) && left + x >= 0 && left + x < cube_display::kWidth &&
            top + y >= 0 && top + y < cube_display::kHeight)
          PixelAt(frame, left + x, top + y) = color;
    left += kAdvance;
  }
}

}  // namespace cube_display::pixel_font

namespace cube_display::compact_font {

constexpr int kWidth = 5;
constexpr int kHeight = 7;

constexpr std::array<uint8_t, kHeight> Glyph(char c) {
  switch (c) {
    case '0': return {14,17,19,21,25,17,14};
    case '1': return {4,12,4,4,4,4,14};
    case '2': return {14,17,1,2,4,8,31};
    case '3': return {30,1,1,14,1,1,30};
    case '4': return {2,6,10,18,31,2,2};
    case '5': return {31,16,16,30,1,1,30};
    case '6': return {14,16,16,30,17,17,14};
    case '7': return {31,1,2,4,4,8,8};
    case '8': return {14,17,17,14,17,17,14};
    case '9': return {14,17,17,15,1,1,14};
    case '-': return {0,0,0,31,0,0,0};
    case '%': return {17,18,2,4,8,9,17};
    case 'A': return {14,17,17,31,17,17,17};
    case 'I': return {31,4,4,4,4,4,31};
    case 'N': return {17,25,21,21,19,17,17};
    case 'O': return {14,17,17,17,17,17,14};
    case 'R': return {30,17,17,30,20,18,17};
    case 'S': return {15,16,16,14,1,1,30};
    case 'W': return {17,17,17,21,21,21,10};
    case ' ': return {};
    default: return {};
  }
}

inline void Draw(Frame64& frame, std::string_view value, int left, int top,
                 int scale, int gap, Rgb color) {
  for (char c : value) {
    const auto rows = Glyph(c);
    for (int y = 0; y < kHeight; ++y)
      for (int x = 0; x < kWidth; ++x)
        if (rows[y] & (16 >> x))
          for (int dy = 0; dy < scale; ++dy)
            for (int dx = 0; dx < scale; ++dx) {
              const int px = left + x * scale + dx;
              const int py = top + y * scale + dy;
              if (px >= 0 && px < cube_display::kWidth && py >= 0 && py < cube_display::kHeight)
                PixelAt(frame, px, py) = color;
            }
    left += kWidth * scale + gap;
  }
}

}  // namespace cube_display::compact_font
