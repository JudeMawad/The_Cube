#pragma once

#include "../music-state.h"

namespace cube_display {
class MusicApp final : public DisplayApp {
 public:
  static constexpr char kId[] = "music";
  explicit MusicApp(const MusicState& state) : state_(state) {}
  bool Available(const DisplayContext& context) const override { return state_.Available(context); }
  void Render(const DisplayContext& context, Frame64* frame) override;
 private:
  const MusicState& state_;
};
}  // namespace cube_display
