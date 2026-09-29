#pragma once

#include "../weather-state.h"

#include <array>
#include <cstddef>

namespace cube_display {

class WeatherApp final : public DisplayApp {
 public:
  static constexpr char kId[] = "weather";
  explicit WeatherApp(const WeatherState& state) : state_(state) {}
  bool Available(const DisplayContext& context) const override { return state_.Available(context); }
  void Render(const DisplayContext& context, Frame64* frame) override;

 private:
  void UpdateText(const WeatherSnapshot& snapshot);
  const WeatherState& state_;
  int cached_temperature_ = 1001;
  int cached_probability_ = -1;
  bool cached_snow_ = false;
  std::array<char, 6> temperature_{};
  std::array<char, 10> probability_{};
  std::size_t temperature_length_ = 0;
  std::size_t probability_length_ = 0;
};

}  // namespace cube_display
