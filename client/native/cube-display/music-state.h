#pragma once

#include "display-runtime.h"

#include <algorithm>
#include <array>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <string_view>

namespace cube_display {

// Same-Pi monotonic deadlines, never backend timestamps. All state is owned by
// the render thread; images arrive as fixed, already prepared RGB24 frames.
class MusicState {
 public:
  static constexpr std::size_t kFrameBytes = 64 * 64 * 3;
  static constexpr std::size_t kMaximumPacket = kFrameBytes + 256;

  bool Apply(std::string_view message, std::chrono::steady_clock::time_point now) {
    constexpr std::string_view prefix = "music v1 ";
    if (message.size() > 256 || message.substr(0, prefix.size()) != prefix) return false;
    message.remove_prefix(prefix.size());
    std::array<std::string_view, 8> tokens{};
    if (!Tokens(message, &tokens)) return false;
    uint64_t epoch, sequence, expires, status;
    int64_t duration, progress;
    if (!Number(tokens[0], &epoch) || !Number(tokens[1], &sequence) ||
        !Number(tokens[2], &expires) || !Number(tokens[3], &status) || status > 3 ||
        !Identity(tokens[4]) || !Identity(tokens[5]) || !Number(tokens[6], &duration) ||
        !Number(tokens[7], &progress) || duration < -1 || progress < -1 ||
        duration > (INT64_C(1) << 53) || progress > (INT64_C(1) << 53) ||
        !epoch || !sequence || epoch < epoch_ || (epoch == epoch_ && sequence <= sequence_)) return false;
    const auto now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(now.time_since_epoch()).count();
    if (status != 0 && (now_ms < 0 || expires <= static_cast<uint64_t>(now_ms) ||
                       expires > static_cast<uint64_t>(now_ms) + 15000 || Zero(tokens[4]))) return false;
    if (status == 0 && (expires != 0 || !Zero(tokens[4]) || !Zero(tokens[5]))) return false;
    const bool same_track = epoch == epoch_ && Equal(track_, tokens[4]);
    const bool was_fresh = now < expires_;
    if (!same_track || !Equal(artwork_id_, tokens[5])) have_artwork_ = false;
    if (status == 1 && (!same_track || !was_fresh || status_ == 0 || status_ == 2))
      play_until_ = now + std::chrono::seconds(5);
    else if (status == 0 || status == 2 || !same_track || !was_fresh)
      play_until_ = {};
    if (status == 2 && (status_ == 1 || status_ == 3) && same_track && was_fresh)
      pause_until_ = now + std::chrono::seconds(5);
    else if (status != 2 || !same_track || !was_fresh)
      pause_until_ = {};
    epoch_ = epoch;
    sequence_ = sequence;
    status_ = static_cast<int>(status);
    std::copy(tokens[4].begin(), tokens[4].end(), track_.begin());
    std::copy(tokens[5].begin(), tokens[5].end(), artwork_id_.begin());
    expires_ = std::chrono::steady_clock::time_point(std::chrono::milliseconds(expires));
    duration_ = duration;
    progress_ = duration >= 0 ? std::min(duration, progress) : progress;
    received_ = now;
    return true;
  }

  bool ApplyArtwork(std::string_view message, std::chrono::steady_clock::time_point now) {
    constexpr std::string_view prefix = "music-art v1 ";
    if (message.size() > kMaximumPacket || message.substr(0, prefix.size()) != prefix) return false;
    message.remove_prefix(prefix.size());
    const auto newline = message.find('\n');
    if (newline == std::string_view::npos || message.size() - newline - 1 != kFrameBytes) return false;
    std::array<std::string_view, 4> tokens{};
    if (!Tokens(message.substr(0, newline), &tokens)) return false;
    uint64_t epoch, sequence;
    if (!Number(tokens[0], &epoch) || !Number(tokens[1], &sequence) || epoch != epoch_ ||
        sequence != sequence_ || !Equal(track_, tokens[2]) || !Equal(artwork_id_, tokens[3]) ||
        Zero(tokens[3]) || status_ == 0 || now >= expires_) return false;
    const auto bytes = message.substr(newline + 1);
    for (std::size_t index = 0; index < artwork_.size(); ++index)
      artwork_[index] = {static_cast<uint8_t>(bytes[3 * index]),
                         static_cast<uint8_t>(bytes[3 * index + 1]),
                         static_cast<uint8_t>(bytes[3 * index + 2])};
    have_artwork_ = true;
    return true;
  }

  bool PauseNotice(const DisplayContext& context) const {
    return status_ == 2 && context.now < expires_ && context.now < pause_until_;
  }
  bool PresentationNotice(const DisplayContext& context) const {
    return PauseNotice(context) || ((status_ == 1 || status_ == 3) &&
        context.now < expires_ && context.now < play_until_);
  }
  bool Available(const DisplayContext& context) const {
    return context.now < expires_ && (status_ == 1 || status_ == 3 || PauseNotice(context));
  }
  int64_t Progress(const DisplayContext& context) const {
    if (progress_ < 0) return -1;
    auto value = progress_;
    if (status_ == 1 && context.now > received_)
      value += std::chrono::duration_cast<std::chrono::milliseconds>(context.now - received_).count();
    return duration_ >= 0 ? std::min(value, duration_) : value;
  }
  int64_t duration() const { return duration_; }
  int status() const { return status_; }
  bool has_artwork() const { return have_artwork_; }
  const Frame64& artwork() const { return artwork_; }

 private:
  template <typename T> static bool Number(std::string_view token, T* value) {
    if (token.empty() || (token.size() > 1 && token[0] == '0') || token == "-0") return false;
    const auto result = std::from_chars(token.data(), token.data() + token.size(), *value);
    return result.ec == std::errc{} && result.ptr == token.data() + token.size();
  }
  template <std::size_t N> static bool Tokens(std::string_view text, std::array<std::string_view, N>* out) {
    for (std::size_t i = 0; i < N; ++i) {
      const auto space = text.find(' ');
      (*out)[i] = text.substr(0, space);
      if ((*out)[i].empty() || (i == N - 1) != (space == std::string_view::npos)) return false;
      if (space != std::string_view::npos) text.remove_prefix(space + 1);
    }
    return true;
  }
  static bool Identity(std::string_view text) {
    return text.size() == 64 && std::all_of(text.begin(), text.end(), [](char c) {
      return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
    });
  }
  static bool Equal(const std::array<char, 64>& id, std::string_view text) {
    return text.size() == id.size() && std::equal(id.begin(), id.end(), text.begin());
  }
  static bool Zero(std::string_view text) {
    return std::all_of(text.begin(), text.end(), [](char c) { return c == '0'; });
  }
  uint64_t epoch_ = 0, sequence_ = 0;
  int status_ = 0;
  int64_t duration_ = -1, progress_ = -1;
  std::array<char, 64> track_{}, artwork_id_{};
  Frame64 artwork_{};
  bool have_artwork_ = false;
  std::chrono::steady_clock::time_point expires_{}, received_{}, pause_until_{}, play_until_{};
};

}  // namespace cube_display
