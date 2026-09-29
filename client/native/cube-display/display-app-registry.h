#pragma once

#include "display-runtime.h"

#include <map>
#include <string>
#include <string_view>
#include <utility>

namespace cube_display {

// Owns IDs, not apps. Registered objects must keep their addresses and outlive
// all registry, scheduler, and runtime use. Registration is setup-time work.
class DisplayAppRegistry {
 public:
  bool Register(std::string id, DisplayApp& app) {
    if (id.empty()) return false;
    return apps_.emplace(std::move(id), &app).second;
  }

  DisplayApp* Find(std::string_view id) const {
    const auto found = apps_.find(id);
    return found == apps_.end() ? nullptr : found->second;
  }

  bool Contains(std::string_view id) const { return Find(id) != nullptr; }

 private:
  std::map<std::string, DisplayApp*, std::less<>> apps_;
};

}  // namespace cube_display
