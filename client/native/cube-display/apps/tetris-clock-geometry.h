#pragma once

#include "../display-frame.h"

#include <array>

namespace cube_display::tetris_clock {

constexpr int kBlock = 2;
constexpr int kColumns = 6;
constexpr int kRows = 14;
constexpr int kTop = 11;
constexpr std::array<int, 4> kDigitX{{3, 17, 35, 49}};
constexpr int kColonX = 31;
// Clock and colon position only; negative moves left, positive moves right.
// Zero leaves three black pixels at each edge of the 64-pixel frame.
constexpr int kClockXOffset = 0;
static_assert(kClockXOffset >= -3 && kClockXOffset <= 3);
constexpr std::array<int, 2> kColonY{{19, 29}};
// Date-only tuning. kDateY moves the stripe and text together; offsets move
// just the black cutout within that stripe. Keep RGB components equal for white.
constexpr int kDateY = 48;
constexpr int kDateStripeHeight = 15;
constexpr int kDateTextXOffset = 0;
constexpr int kDateTextYOffset = 0;
constexpr Rgb kDateStripeColor{255, 255, 255};
constexpr Rgb kNeutral{224, 224, 224};
constexpr std::array<Rgb, 7> kPalette{{
    {0, 220, 240}, {48, 96, 255}, {255, 144, 32}, {248, 224, 32},
    {48, 224, 88}, {184, 72, 240}, {248, 56, 64}}};

struct Cell { int x = 0, y = 0; };
struct Piece { std::array<Cell, 4> cells{}; };
struct Digit {
  std::array<Piece, 17> pieces{};
  int count = 0;
};

// Original stroke tessellation: each occupied cell belongs to one tetromino.
// Bands use two mirrored L pieces over an I; stems use interlocking L pairs.
// Append lower targets first so construction moves upward. No runtime solver.
constexpr void Add(Digit& digit, Cell a, Cell b, Cell c, Cell d) {
  digit.pieces[digit.count++] = Piece{{a, b, c, d}};
}

constexpr void Square(Digit& digit, int x, int y) {
  Add(digit, {x, y}, {x + 1, y}, {x, y + 1}, {x + 1, y + 1});
}

constexpr void Band(Digit& digit, int y, bool across, bool left, bool right) {
  if (across) {
    Add(digit, {1, y + 1}, {2, y + 1}, {3, y + 1}, {4, y + 1});
    Add(digit, {0, y}, {1, y}, {2, y}, {0, y + 1});
    Add(digit, {3, y}, {4, y}, {5, y}, {5, y + 1});
  } else {
    if (left) Square(digit, 0, y);
    if (right) Square(digit, 4, y);
  }
}

constexpr void Stem(Digit& digit, int x, int y) {
  Add(digit, {x + 1, y + 1}, {x + 1, y + 2}, {x, y + 3}, {x + 1, y + 3});
  Add(digit, {x, y}, {x + 1, y}, {x, y + 1}, {x, y + 2});
}

constexpr Digit MakeDigit(bool top, bool upper_left, bool upper_right,
                          bool middle, bool lower_left, bool lower_right, bool bottom) {
  Digit digit;
  Band(digit, 12, bottom, lower_left, lower_right);
  if (lower_left) Stem(digit, 0, 8);
  if (lower_right) Stem(digit, 4, 8);
  Band(digit, 6, middle, upper_left || lower_left, upper_right || lower_right);
  if (upper_left) Stem(digit, 0, 2);
  if (upper_right) Stem(digit, 4, 2);
  Band(digit, 0, top, upper_left, upper_right);
  return digit;
}

// A centered stem and full-width foot give 1 the same visual bounds as the
// other digits. The small upper-left cap makes it read as a numeral, not a bar.
constexpr Digit MakeOne() {
  Digit digit;
  Band(digit, 12, true, false, false);
  Stem(digit, 2, 8);
  Square(digit, 2, 6);
  Stem(digit, 2, 2);
  Square(digit, 1, 0);
  return digit;
}

constexpr std::array<Digit, 10> kDigits{{
    MakeDigit(true,  true,  true,  false, true,  true,  true),  // 0
    MakeOne(),                                                  // 1
    MakeDigit(true,  false, true,  true,  true,  false, true),  // 2
    MakeDigit(true,  false, true,  true,  false, true,  true),  // 3
    MakeDigit(false, true,  true,  true,  false, true,  false), // 4
    MakeDigit(true,  true,  false, true,  false, true,  true),  // 5
    MakeDigit(true,  true,  false, true,  true,  true,  true),  // 6
    MakeDigit(true,  false, true,  false, false, true,  false), // 7
    MakeDigit(true,  true,  true,  true,  true,  true,  true),  // 8
    MakeDigit(true,  true,  true,  true,  false, true,  true),  // 9
}};

}  // namespace cube_display::tetris_clock
