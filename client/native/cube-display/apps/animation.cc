#include "animation.h"

#include <algorithm>
#include <chrono>
#include <cstdint>

namespace cube_display {

void AnimationApp::OnActivated(const DisplayContext& context) {
  if (!Available(context)) return;
  selected_ = activated_ ? (selected_ + 1) % catalog_.size() : 0;
  started_ = context.now;
  activated_ = true;
}

void AnimationApp::Render(const DisplayContext& context, Frame64* frame) {
  if (!activated_ || !Available(context)) return;
  const auto& asset = catalog_.at(selected_);
  const auto age = std::max(context.now - started_,
                            std::chrono::steady_clock::duration::zero());
  const auto milliseconds =
      std::chrono::duration_cast<std::chrono::milliseconds>(age).count();
  const auto phase = static_cast<std::uint32_t>(milliseconds % asset.total_ms);
  const auto end = asset.frame_starts.begin() + asset.frame_count;
  const auto frame_index = static_cast<std::size_t>(
      std::upper_bound(asset.frame_starts.begin(), end, phase) -
      asset.frame_starts.begin() - 1);
  const auto offset = kAnimationHeaderBytes + frame_index * kAnimationFrameBytes + 2;
  for (std::size_t i = 0; i < kPixels; ++i) {
    const auto color = static_cast<std::uint16_t>(
        asset.bytes[offset + i * 2] | (asset.bytes[offset + i * 2 + 1] << 8));
    const auto red = static_cast<std::uint8_t>((color >> 11) & 31);
    const auto green = static_cast<std::uint8_t>((color >> 5) & 63);
    const auto blue = static_cast<std::uint8_t>(color & 31);
    (*frame)[i] = {static_cast<std::uint8_t>((red << 3) | (red >> 2)),
                   static_cast<std::uint8_t>((green << 2) | (green >> 4)),
                   static_cast<std::uint8_t>((blue << 3) | (blue >> 2))};
  }
}

}  // namespace cube_display
