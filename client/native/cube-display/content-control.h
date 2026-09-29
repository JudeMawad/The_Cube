#pragma once

#include "content-renderer.h"
#include <string_view>

namespace cube_content {

// Transport credentials/truncation stay in the existing receiver. Old display
// replies remain untouched; diagnostics have their own local-only command.
inline std::string ApplyContentCommand(std::string_view message,
                                       ContentRenderer& renderer, bool available) {
  if (message.size() > 128) return "{}";
  if (message == "control get_content_status") {
    return available ? renderer.Status() : "{\"available\":false}";
  }
  if (!available) return "{}";
  if (message == "control clear_content") {
    renderer.ClearContent();
    return "{\"accepted\":true}";
  }
  constexpr std::string_view text_prefix = "control show_text ";
  constexpr std::string_view icon_prefix = "control show_icon ";
  Mask64 mask{};
  bool valid = false;
  if (message.substr(0, text_prefix.size()) == text_prefix) {
    valid = TextMask(message.substr(text_prefix.size()), &mask);
  } else if (message.substr(0, icon_prefix.size()) == icon_prefix) {
    valid = IconMask(message.substr(icon_prefix.size()), &mask);
  }
  return valid && renderer.RequestMask(mask) ? "{\"accepted\":true}" : "{}";
}

}  // namespace cube_content
