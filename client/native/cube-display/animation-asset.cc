#include "animation-asset.h"

#include <algorithm>
#include <cstring>
#include <fstream>
#include <iostream>
#include <system_error>

namespace cube_display {
namespace {

constexpr char kMagic[] = "CUBEANIM";

std::uint16_t Read16(const std::vector<std::uint8_t>& bytes, std::size_t offset) {
  return static_cast<std::uint16_t>(bytes[offset] | (bytes[offset + 1] << 8));
}

bool Reject(std::string message, std::string* error) {
  if (error) *error = std::move(message);
  return false;
}

}  // namespace

bool LoadAnimationFile(const std::filesystem::path& path, AnimationAsset* asset,
                       std::string* error) {
  if (!asset) return Reject("null destination", error);
  std::error_code ec;
  const auto size = std::filesystem::file_size(path, ec);
  if (ec || size < kAnimationHeaderBytes || size > kAnimationMaxBytes)
    return Reject("invalid file size", error);
  std::ifstream input(path, std::ios::binary);
  if (!input) return Reject("cannot open file", error);
  AnimationAsset candidate;
  candidate.bytes.resize(static_cast<std::size_t>(size));
  input.read(reinterpret_cast<char*>(candidate.bytes.data()),
             static_cast<std::streamsize>(candidate.bytes.size()));
  if (!input || input.gcount() != static_cast<std::streamsize>(candidate.bytes.size()))
    return Reject("truncated file", error);
  const auto& bytes = candidate.bytes;
  if (std::memcmp(bytes.data(), kMagic, sizeof(kMagic) - 1) != 0)
    return Reject("bad magic", error);
  if (Read16(bytes, 8) != 1) return Reject("unsupported version", error);
  if (Read16(bytes, 10) != kWidth || Read16(bytes, 12) != kHeight)
    return Reject("invalid dimensions", error);
  const auto count = Read16(bytes, 14);
  if (count == 0 || count > kAnimationMaxFrames)
    return Reject("invalid frame count", error);
  // Both operands are bounded before multiplication; uint64 keeps the size
  // calculation safe even if format limits change later.
  const std::uint64_t expected = kAnimationHeaderBytes +
      static_cast<std::uint64_t>(count) * kAnimationFrameBytes;
  if (expected != size) return Reject("payload length mismatch", error);
  std::uint32_t total = 0;
  for (std::size_t i = 0; i < count; ++i) {
    const auto duration = Read16(bytes, kAnimationHeaderBytes + i * kAnimationFrameBytes);
    if (duration < kAnimationMinFrameMs || duration > kAnimationMaxFrameMs)
      return Reject("invalid frame duration", error);
    candidate.frame_starts[i] = total;
    total += duration;
    if (total > kAnimationMaxTotalMs)
      return Reject("animation duration exceeds limit", error);
  }
  candidate.frame_count = count;
  candidate.total_ms = total;
  candidate.name = path.stem().string();
  *asset = std::move(candidate);
  if (error) error->clear();
  return true;
}

void AnimationCatalog::LoadDirectory(const std::filesystem::path& directory) {
  assets_.clear();
  assets_.reserve(kAnimationMaxAssets);
  std::error_code ec;
  std::filesystem::directory_iterator iterator(directory, ec);
  if (ec) {
    std::cerr << "Animation directory unavailable: " << directory << ": "
              << ec.message() << '\n';
    return;
  }
  std::size_t ignored = 0;
  const std::filesystem::directory_iterator end;
  for (; iterator != end; iterator.increment(ec)) {
    const auto& entry = *iterator;
    if (entry.path().extension() != ".cubeanim" || !entry.is_regular_file(ec) || ec) {
      ec.clear();
      continue;
    }
    const auto path = entry.path();
    const auto name = path.stem().string();
    if (assets_.size() == kAnimationMaxAssets && name >= assets_.back().name) {
      ++ignored;
      continue;
    }
    AnimationAsset asset;
    std::string error;
    if (!LoadAnimationFile(path, &asset, &error)) {
      std::cerr << "Skipping animation " << path << ": " << error << '\n';
      continue;
    }
    const auto position = std::lower_bound(
        assets_.begin(), assets_.end(), name,
        [](const AnimationAsset& item, const std::string& candidate) {
          return item.name < candidate;
        });
    assets_.insert(position, std::move(asset));
    if (assets_.size() > kAnimationMaxAssets) {
      assets_.pop_back();
      ++ignored;
    }
  }
  if (ec) std::cerr << "Animation directory scan failed: " << ec.message() << '\n';
  if (ignored)
    std::cerr << "Animation catalog limit reached; ignored " << ignored
              << " additional .cubeanim files\n";
}

}  // namespace cube_display
