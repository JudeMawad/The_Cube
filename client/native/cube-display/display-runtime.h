#pragma once

#include "display-frame.h"

#include <chrono>

namespace cube_display {

struct DisplayContext {
  std::chrono::steady_clock::time_point now;
  std::chrono::steady_clock::duration elapsed;
};

class DisplayApp {
 public:
  virtual ~DisplayApp() = default;
  virtual bool Available(const DisplayContext& context) const = 0;
  // Called once before the first visible render after the active app changes.
  virtual void OnActivated(const DisplayContext&) {}
  virtual void Render(const DisplayContext& context, Frame64* frame) = 0;
};

// The caller owns the app. A future scheduler only needs to change this slot.
class DisplayRuntime {
 public:
  void SetActiveApp(DisplayApp* app) {
    if (active_ != app) {
      active_ = app;
      activation_pending_ = app != nullptr;
    }
  }
  const Frame64& Render(const DisplayContext& context) {
    frame_.fill({});
    if (active_ && active_->Available(context)) {
      if (activation_pending_) {
        active_->OnActivated(context);
        activation_pending_ = false;
      }
      active_->Render(context, &frame_);
    }
    return frame_;
  }

 private:
  DisplayApp* active_ = nullptr;
  bool activation_pending_ = false;
  Frame64 frame_{};
};

}  // namespace cube_display
