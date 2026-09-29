#pragma once
#include <string>

// Hardware-independent state and strict protocol; no power commands exist here.
struct DisplayControl {
  bool display_enabled = true;
  int master_brightness_percent = 10;

  bool visible() const {
    return display_enabled && master_brightness_percent > 0;
  }

  bool Apply(const std::string& message) {
    if (message == "control get_status") return true;
    if (message == "control set_display on") display_enabled = true;
    else if (message == "control set_display off") display_enabled = false;
    else {
      const std::string prefix = "control set_brightness ";
      if (message.compare(0, prefix.size(), prefix) != 0) return false;
      const auto value = message.substr(prefix.size());
      if (value.empty() || value.size() > 3) return false;
      int percent = 0;
      for (char c : value) {
        if (c < '0' || c > '9') return false;
        percent = percent * 10 + c - '0';
      }
      if (percent > 100 || std::to_string(percent) != value) return false;
      master_brightness_percent = percent;
    }
    return true;
  }

  std::string Status() const {
    return std::string("{\"display_enabled\":") + (display_enabled ? "true" : "false") +
        ",\"master_brightness_percent\":" + std::to_string(master_brightness_percent) + "}";
  }
};
