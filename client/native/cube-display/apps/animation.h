#pragma once

#include "../animation-asset.h"
#include "../display-runtime.h"

#include <cstddef>

namespace cube_display {

class AnimationApp final : public DisplayApp {
 public:
  static constexpr char kId[] = "animation";
  explicit AnimationApp(const AnimationCatalog& catalog) : catalog_(catalog) {}
  bool Available(const DisplayContext&) const override { return catalog_.size() != 0; }
  void OnActivated(const DisplayContext& context) override;
  void Render(const DisplayContext& context, Frame64* frame) override;

 private:
  const AnimationCatalog& catalog_;
  std::size_t selected_ = 0;
  std::chrono::steady_clock::time_point started_{};
  bool activated_ = false;
};

}  // namespace cube_display
