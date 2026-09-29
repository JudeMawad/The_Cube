#pragma once

#include "display-app-registry.h"

#include <chrono>
#include <set>
#include <string>
#include <string_view>
#include <vector>

namespace cube_display {

struct CarouselEntry {
  std::string app_id;
  std::chrono::steady_clock::duration duration;
};

// Single-threaded selection only. Registry and registered apps must outlive
// this scheduler. Configure and Update run on the caller's render thread.
class CarouselScheduler {
 public:
  explicit CarouselScheduler(const DisplayAppRegistry& registry) : registry_(registry) {}

  // Resolve once, outside the frame path. Rejection leaves timing and selection
  // untouched; acceptance starts a new schedule on the next Update.
  bool Configure(const std::vector<CarouselEntry>& entries, std::string* error = nullptr) {
    std::vector<ResolvedEntry> resolved;
    resolved.reserve(entries.size());
    std::set<std::string_view> ids;
    for (const auto& entry : entries) {
      std::string reason;
      if (entry.app_id.empty()) reason = "empty app ID";
      else if (entry.duration <= Duration::zero()) reason = "nonpositive duration";
      else if (!ids.insert(entry.app_id).second) reason = "duplicate app ID";
      DisplayApp* app = registry_.Find(entry.app_id);
      if (reason.empty() && !app) reason = "unknown app ID";
      if (!reason.empty()) {
        if (error) *error = reason + ": " + entry.app_id;
        return false;
      }
      resolved.push_back({app, entry.duration});
    }
    entries_.swap(resolved);
    selected_ = entries_.size();
    remaining_ = Duration::zero();
    have_tick_ = false;
    if (error) error->clear();
    return true;
  }

  DisplayApp* Update(const DisplayContext& context, bool presentation_suspended) {
    // The previous suspension state describes the interval just completed.
    // Charge time up to pause entry, but never charge a paused interval on exit.
    if (have_tick_ && !suspended_ && context.now > previous_tick_) {
      const auto elapsed = context.now - previous_tick_;
      remaining_ = elapsed >= remaining_ ? Duration::zero() : remaining_ - elapsed;
    }
    previous_tick_ = context.now;
    have_tick_ = true;
    suspended_ = presentation_suspended;

    if (selected_ == entries_.size()) return SelectFrom(0, context);
    if (!entries_[selected_].app->Available(context))
      return SelectFrom((selected_ + 1) % entries_.size(), context);
    if (!suspended_ && remaining_ == Duration::zero())
      return SelectFrom((selected_ + 1) % entries_.size(), context);
    return entries_[selected_].app;
  }

 private:
  using Duration = std::chrono::steady_clock::duration;
  struct ResolvedEntry {
    DisplayApp* app;
    Duration duration;
  };

  DisplayApp* SelectFrom(std::size_t start, const DisplayContext& context) {
    // Scan once, including the old app last when it is the only available app.
    // A selection always gets a full dwell; elapsed overshoot is discarded.
    for (std::size_t count = 0; count < entries_.size(); ++count) {
      const auto index = (start + count) % entries_.size();
      if (!entries_[index].app->Available(context)) continue;
      selected_ = index;
      remaining_ = entries_[index].duration;
      return entries_[index].app;
    }
    selected_ = entries_.size();
    remaining_ = Duration::zero();
    return nullptr;
  }

  const DisplayAppRegistry& registry_;
  std::vector<ResolvedEntry> entries_;
  std::size_t selected_ = 0;
  Duration remaining_{};
  std::chrono::steady_clock::time_point previous_tick_{};
  bool have_tick_ = false;
  bool suspended_ = false;
};

}  // namespace cube_display
