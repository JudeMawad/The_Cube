#pragma once

#include "display-runtime.h"

#include <chrono>
#include <cstdint>
#include <limits>
#include <string_view>

namespace cube_display {

enum class WeatherCondition : int {
  Clear, PartlyCloudy, Cloudy, Rain, HeavyRain, Thunderstorm, Snow, Fog
};

struct WeatherSnapshot {
  int temperature_c10 = 0;
  int precipitation_probability = 0;
  WeatherCondition condition = WeatherCondition::Cloudy;
  bool is_day = true;
};

// Updated and read only by the renderer thread. A repeated datagram may shrink,
// but cannot extend, the validity supplied by the Python provider.
class WeatherState {
 public:
  static constexpr auto kMaximumLease = std::chrono::seconds(1800);

  bool Apply(std::string_view message, std::chrono::steady_clock::time_point now) {
    constexpr std::string_view prefix = "weather v1 ";
    if (message.substr(0, prefix.size()) != prefix) return false;
    message.remove_prefix(prefix.size());
    int values[5]{};
    for (int index = 0; index < 5; ++index) {
      const auto separator = message.find(' ');
      const auto token = message.substr(0, separator);
      if (!ParseDecimal(token, &values[index])) return false;
      if (index != 4) {
        if (separator == std::string_view::npos) return false;
        message.remove_prefix(separator + 1);
      } else if (separator != std::string_view::npos) return false;
    }
    if (values[0] < -1000 || values[0] > 1000 || values[1] < 0 || values[1] > 100 ||
        values[2] < 0 || values[2] > 7 || values[3] < 0 || values[3] > 1 ||
        values[4] < 1 || values[4] > kMaximumLease.count()) return false;
    snapshot_ = {values[0], values[1], static_cast<WeatherCondition>(values[2]),
                 values[3] == 1};
    expires_at_ = now + std::chrono::seconds(values[4]);
    valid_ = true;
    return true;
  }

  bool Available(const DisplayContext& context) const {
    return valid_ && context.now < expires_at_;
  }
  const WeatherSnapshot& snapshot() const { return snapshot_; }

 private:
  static bool ParseDecimal(std::string_view token, int* result) {
    if (token.empty()) return false;
    const bool negative = token.front() == '-';
    if (negative) token.remove_prefix(1);
    if (token.empty() || (token.size() > 1 && token.front() == '0') ||
        (negative && token == "0")) return false;
    int number = 0;
    for (char digit : token) {
      if (digit < '0' || digit > '9' ||
          number > (std::numeric_limits<int>::max() - (digit - '0')) / 10) return false;
      number = number * 10 + digit - '0';
    }
    *result = negative ? -number : number;
    return true;
  }

  WeatherSnapshot snapshot_{};
  std::chrono::steady_clock::time_point expires_at_{};
  bool valid_ = false;
};

}  // namespace cube_display
