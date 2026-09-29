#pragma once

#include "display-frame.h"

#include <array>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <string>
#include <vector>

namespace cube_display {

constexpr std::size_t kAnimationMaxFrames = 64;
constexpr std::size_t kAnimationMaxAssets = 8;
constexpr std::uint16_t kAnimationMinFrameMs = 20;
constexpr std::uint16_t kAnimationMaxFrameMs = 2000;
constexpr std::uint32_t kAnimationMaxTotalMs = 60000;
constexpr std::size_t kAnimationHeaderBytes = 16;
constexpr std::size_t kAnimationFrameBytes = 2 + kPixels * 2;
constexpr std::size_t kAnimationMaxBytes =
    kAnimationHeaderBytes + kAnimationMaxFrames * kAnimationFrameBytes;

// Validated bytes remain immutable after startup. Frame offsets and durations
// are derived once, so rendering does no I/O or allocation.
struct AnimationAsset {
  std::string name;
  std::vector<std::uint8_t> bytes;
  std::array<std::uint32_t, kAnimationMaxFrames> frame_starts{};
  std::uint16_t frame_count = 0;
  std::uint32_t total_ms = 0;
};

bool LoadAnimationFile(const std::filesystem::path& path, AnimationAsset* asset,
                       std::string* error = nullptr);

class AnimationCatalog {
 public:
  void LoadDirectory(const std::filesystem::path& directory);
  std::size_t size() const { return assets_.size(); }
  const AnimationAsset& at(std::size_t index) const { return assets_.at(index); }

 private:
  std::vector<AnimationAsset> assets_;
};

}  // namespace cube_display
