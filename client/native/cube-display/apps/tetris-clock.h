#pragma once

#include "../display-runtime.h"

#include <array>
#include <ctime>
#include <string_view>

namespace cube_display {

class LocalClockSource {
 public:
  virtual ~LocalClockSource() = default;
  virtual bool Read(std::tm* local) const = 0;
};

class SystemLocalClock final : public LocalClockSource {
 public:
  bool Read(std::tm* local) const override;
};

class TetrisClockApp final : public DisplayApp {
 public:
  static constexpr char kId[] = "tetris-clock";
  // The source must outlive the app. All state and rendering stay on the caller's thread.
  explicit TetrisClockApp(const LocalClockSource& clock) : clock_(clock) {}
  bool Available(const DisplayContext&) const override { return true; }
  void Render(const DisplayContext& context, Frame64* frame) override;

  const std::array<int, 4>& target_digits() const { return digits_; }
  std::string_view date_text() const { return {date_.data(), date_.size() - 1}; }

 private:
  const LocalClockSource& clock_;
  std::array<int, 4> digits_{{-1, -1, -1, -1}};
  std::array<std::chrono::steady_clock::time_point, 4> started_{};
  std::array<char, 11> date_{};
  std::array<int, 3> date_key_{{-1, -1, -1}};
};

}  // namespace cube_display
