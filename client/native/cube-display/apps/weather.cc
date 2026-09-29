#include "weather.h"
#include "../pixel-font.h"

#include <algorithm>
#include <array>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <string_view>

namespace cube_display {
namespace {

constexpr int kSceneLeft = 4, kSceneRight = 59, kSceneTop = 0, kSceneBottom = 27;
constexpr int kSceneYOffset = -2;
constexpr int kTemperatureTop = 30;
constexpr int kStripeY = 48, kStripeHeight = 15; // same bounds as the Clock date stripe
constexpr int kStripeTextY = kStripeY + (kStripeHeight - pixel_font::kHeight) / 2;
static_assert(kTemperatureTop + 2 * compact_font::kHeight - 1 == 43);
static_assert(kStripeTextY == 51 && kStripeY + kStripeHeight == kHeight - 1);
constexpr Rgb kSun{255, 196, 29}, kRay{255, 151, 22}, kMoon{210, 224, 255};
constexpr Rgb kStar{161, 203, 255}, kStarDim{57, 81, 117};
constexpr Rgb kCloudBack{74, 111, 151}, kCloudFront{149, 190, 225};
constexpr Rgb kStormCloud{64, 89, 119}, kRain{42, 182, 255};
constexpr Rgb kHeavyDrop{74, 224, 255}, kSnow{230, 251, 255};
constexpr Rgb kFog{91, 122, 151}, kLightning{255, 241, 120};
constexpr Rgb kTemperature{255, 247, 218}, kStripeBlue{48, 128, 255};
constexpr std::array<int, 12> kParticleX{{10, 15, 20, 25, 30, 35, 40, 45, 50, 54, 12, 48}};
constexpr std::array<int, 12> kParticlePhase{{1, 12, 5, 19, 9, 25, 3, 17, 29, 8, 22, 14}};

void Dot(Frame64& frame, int x, int y, Rgb color) {
  if (x >= kSceneLeft && x <= kSceneRight && y >= kSceneTop && y <= kSceneBottom)
    frame[PixelIndex(x, y)] = color;
}

void Rect(Frame64& frame, int x, int y, int width, int height, Rgb color) {
  for (int row = std::max(kSceneTop, y); row < std::min(kSceneBottom + 1, y + height); ++row)
    for (int col = std::max(kSceneLeft, x); col < std::min(kSceneRight + 1, x + width); ++col)
      frame[PixelIndex(col, row)] = color;
}

void Disk(Frame64& frame, int cx, int cy, int radius, Rgb color) {
  for (int dy = -radius; dy <= radius; ++dy)
    for (int dx = -radius; dx <= radius; ++dx)
      if (dx * dx + dy * dy <= radius * radius) Dot(frame, cx + dx, cy + dy, color);
}

void Cloud(Frame64& frame, int x, int y, int width, Rgb color) {
  Rect(frame, x + 4, y + 7, width - 8, 11, color);
  Rect(frame, x + 7, y + 4, width - 15, 13, color);
  Rect(frame, x + 12, y + 1, width / 3, 15, color);
  Rect(frame, x + width / 2, y + 3, width / 3 - 2, 13, color);
  Rect(frame, x + 2, y + 11, width - 4, 6, color);
}

int64_t Milliseconds(const DisplayContext& context) {
  return std::chrono::duration_cast<std::chrono::milliseconds>(context.now.time_since_epoch()).count();
}

int Triangle(int64_t milliseconds, int64_t period, int amplitude) {
  const int64_t position = (milliseconds % period + period) % period;
  const int64_t half = period / 2;
  return static_cast<int>((position <= half ? position : period - position) *
                          (2 * amplitude) / half) - amplitude;
}

void Sun(Frame64& frame, int64_t ms, int cx, int cy, int radius = 9) {
  Disk(frame, cx, cy, radius, kSun);
  const int ray = radius + 3 + static_cast<int>((ms / 900) % 2);
  for (int direction : {-1, 1}) {
    Rect(frame, cx + direction * ray - 1, cy - 1, 3, 3, kRay);
    Rect(frame, cx - 1, cy + direction * ray - 1, 3, 3, kRay);
    Rect(frame, cx + direction * (ray - 2) - 1, cy + direction * (ray - 2) - 1,
         3, 3, kRay);
    Rect(frame, cx + direction * (ray - 2) - 1, cy - direction * (ray - 2) - 1,
         3, 3, kRay);
  }
}

void Moon(Frame64& frame, int64_t ms, int cx, int cy) {
  Disk(frame, cx, cy, 10, kMoon);
  Disk(frame, cx + 5, cy - 3, 9, {});
  constexpr std::array<std::array<int, 2>, 5> stars{{{{11, 8}}, {{49, 6}}, {{55, 24}},
                                                       {{13, 28}}, {{46, 31}}}};
  for (std::size_t i = 0; i < stars.size(); ++i) {
    const auto color = ((ms / 1100 + static_cast<int64_t>(i) * 2) % 5 < 2) ? kStar : kStarDim;
    Dot(frame, stars[i][0], stars[i][1], color);
  }
}

int Density(int probability, int sparse, int medium, int dense) {
  return probability <= 33 ? sparse : probability <= 66 ? medium : dense;
}

void Precipitation(Frame64& frame, int64_t ms, int probability, bool heavy, bool snow) {
  const int count = heavy ? Density(probability, 6, 9, 12) : Density(probability, 2, 5, 8);
  const int speed = snow ? 220 : heavy ? 60 : 100;
  for (int i = 0; i < count; ++i) {
    const int step = static_cast<int>((ms / speed + kParticlePhase[i]) % 18);
    const int y = 12 + kSceneYOffset + step;
    const int drift = snow ? Triangle(ms + i * 600, 5000, 2) : 0;
    const int x = kParticleX[i] + drift;
    if (snow) {
      Dot(frame, x, y, kSnow);
      Dot(frame, x - 1, y, kSnow);
      Dot(frame, x + 1, y, kSnow);
      Dot(frame, x, y - 1, kSnow);
      Dot(frame, x, y + 1, kSnow);
    } else {
      Dot(frame, x, y, heavy ? kHeavyDrop : kRain);
      Dot(frame, x - 1, y + 1, heavy ? kHeavyDrop : kRain);
      if (heavy) Dot(frame, x - 2, y + 2, kHeavyDrop);
    }
  }
}

void Scene(Frame64& frame, const WeatherSnapshot& weather, int64_t ms) {
  switch (weather.condition) {
    case WeatherCondition::Clear:
      if (weather.is_day) Sun(frame, ms, 31, 15 + kSceneYOffset);
      else Moon(frame, ms, 31, 14 + kSceneYOffset);
      break;
    case WeatherCondition::PartlyCloudy:
      if (weather.is_day) Sun(frame, ms, 25, 11 + kSceneYOffset, 8);
      else Moon(frame, ms, 25, 11 + kSceneYOffset);
      Cloud(frame, 24 + Triangle(ms, 12000, 3), 9 + kSceneYOffset, 34, kCloudFront);
      break;
    case WeatherCondition::Cloudy:
      Cloud(frame, 7 + Triangle(ms, 16000, 4), 2 + kSceneYOffset, 35, kCloudBack);
      Cloud(frame, 25 + Triangle(ms, 24000, 4), 9 + kSceneYOffset, 34, kCloudFront);
      break;
    case WeatherCondition::Rain:
    case WeatherCondition::HeavyRain:
    case WeatherCondition::Thunderstorm: {
      const bool heavy = weather.condition != WeatherCondition::Rain;
      Precipitation(frame, ms, weather.precipitation_probability, heavy, false);
      Cloud(frame, 12, 1 + kSceneYOffset, 40, heavy ? kStormCloud : kCloudFront);
      if (weather.condition == WeatherCondition::Thunderstorm && ms % 8000 < 180) {
        Rect(frame, 30, 13 + kSceneYOffset, 5, 4, kLightning);
        Rect(frame, 27, 17 + kSceneYOffset, 5, 4, kLightning);
        Rect(frame, 30, 21 + kSceneYOffset, 4, 4, kLightning);
        Rect(frame, 28, 25 + kSceneYOffset, 3, 3, kLightning);
      }
      break;
    }
    case WeatherCondition::Snow:
      Precipitation(frame, ms, weather.precipitation_probability, false, true);
      Cloud(frame, 12, 1 + kSceneYOffset, 40, kCloudFront);
      break;
    case WeatherCondition::Fog:
      for (int layer = 0; layer < 3; ++layer) {
        const int shift = Triangle(ms + layer * 2500, 20000 + layer * 8000, 6);
        const int y = 3 + kSceneYOffset + layer * 9;
        Rect(frame, 8 + shift, y, 38, 3, kFog);
        Rect(frame, 19 - shift, y + 4, 37, 2, kCloudBack);
      }
      break;
  }
}

}  // namespace

void WeatherApp::UpdateText(const WeatherSnapshot& snapshot) {
  const int value = snapshot.temperature_c10;
  if (value != cached_temperature_) {
    cached_temperature_ = value;
    const int rounded = value < 0 ? -((-value + 5) / 10) : (value + 5) / 10;
    const auto result = std::to_chars(temperature_.data(), temperature_.data() + temperature_.size(), rounded);
    temperature_length_ = static_cast<std::size_t>(result.ptr - temperature_.data());
  }
  const bool snow = snapshot.condition == WeatherCondition::Snow;
  if (snapshot.precipitation_probability != cached_probability_ || snow != cached_snow_) {
    cached_probability_ = snapshot.precipitation_probability;
    cached_snow_ = snow;
    constexpr std::string_view rain = "RAIN ", snowfall = "SNOW ";
    const auto prefix = snow ? snowfall : rain;
    std::copy(prefix.begin(), prefix.end(), probability_.begin());
    const auto start = probability_.data() + prefix.size();
    const auto result = std::to_chars(start, probability_.data() + probability_.size() - 1,
                                      cached_probability_);
    *result.ptr = '%';
    probability_length_ = static_cast<std::size_t>(result.ptr - probability_.data()) + 1;
  }
}

void WeatherApp::Render(const DisplayContext& context, Frame64* frame) {
  frame->fill({});
  if (!Available(context)) return;
  const auto& weather = state_.snapshot();
  Scene(*frame, weather, Milliseconds(context));
  UpdateText(weather);
  const int number_width = static_cast<int>(temperature_length_) * 12 - 2;
  const int left = (kWidth - number_width) / 2;
  compact_font::Draw(*frame, {temperature_.data(), temperature_length_}, left,
                     kTemperatureTop, 2, 2, kTemperature);
  const int degree_x = left + number_width + 2;
  PixelAt(*frame, degree_x + 1, kTemperatureTop) = kTemperature;
  PixelAt(*frame, degree_x + 2, kTemperatureTop) = kTemperature;
  PixelAt(*frame, degree_x, kTemperatureTop + 1) = kTemperature;
  PixelAt(*frame, degree_x + 3, kTemperatureTop + 1) = kTemperature;
  PixelAt(*frame, degree_x + 1, kTemperatureTop + 2) = kTemperature;
  PixelAt(*frame, degree_x + 2, kTemperatureTop + 2) = kTemperature;
  for (int y = kStripeY; y < kStripeY + kStripeHeight; ++y)
    for (int x = 0; x < kWidth; ++x)
      (*frame)[PixelIndex(x, y)] = kStripeBlue;
  const int caption_width = static_cast<int>(probability_length_) * pixel_font::kAdvance - 1;
  pixel_font::Draw(*frame, {probability_.data(), probability_length_},
                   (kWidth - caption_width) / 2, kStripeTextY, {});
}

}  // namespace cube_display
