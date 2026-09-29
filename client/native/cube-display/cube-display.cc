#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include "led-matrix.h"
#include "display-control.h"
#include "display-runtime.h"
#include "carousel-scheduler.h"
#include "display-compositor.h"
#include "content-control.h"
#include "apps/tetris-clock.h"
#include "apps/weather.h"
#include "apps/animation.h"
#include "apps/music.h"
#include "animation-asset.h"
#include "weather-state.h"

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <string>
#include <thread>

#include <sys/socket.h>
#include <sys/un.h>
#include <pwd.h>
#include <unistd.h>

using rgb_matrix::FrameCanvas;
using rgb_matrix::RGBMatrix;

namespace {

volatile std::sig_atomic_t g_stop = 0;
constexpr char kControlSocketName[] = "cube-display";

struct PresentationControl {
  DisplayControl display;
  cube_display::CubeState state = cube_display::CubeState::Idle;
  double speech_level = 0;
  std::chrono::steady_clock::time_point last_voice_update =
      std::chrono::steady_clock::now();
  std::chrono::steady_clock::time_point last_state_update =
      std::chrono::steady_clock::now();
  std::chrono::steady_clock::time_point state_started_at =
      std::chrono::steady_clock::now();
};

void SetPresentationState(cube_display::CubeState state,
                          std::chrono::steady_clock::time_point now,
                          PresentationControl* control) {
  if (control->state != state) {
    control->state = state;
    control->state_started_at = now;
  }
}

void HandleSignal(int) { g_stop = 1; }

int CreateControlSocket(uid_t* allowed_uid) {
  errno = 0;
  const char* name = std::getenv("CUBE_DISPLAY_USER");
  if (name == nullptr || *name == '\0') {
    std::cerr << "CUBE_DISPLAY_USER must name the Pi voice account\n";
    return -1;
  }
  const passwd* voice_user = getpwnam(name);
  if (voice_user == nullptr) {
    std::cerr << "Unable to resolve configured display-control user: "
              << (errno == 0 ? "user not found" : std::strerror(errno)) << '\n';
    return -1;
  }
  *allowed_uid = voice_user->pw_uid;

  const int socket_fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
  if (socket_fd < 0) {
    std::cerr << "Unable to create display control socket: " << std::strerror(errno) << '\n';
    return -1;
  }
  const int pass_credentials = 1;
  if (setsockopt(socket_fd, SOL_SOCKET, SO_PASSCRED,
                 &pass_credentials, sizeof(pass_credentials)) < 0) {
    std::cerr << "Unable to enable display-control credentials: "
              << std::strerror(errno) << '\n';
    close(socket_fd);
    return -1;
  }

  sockaddr_un address{};
  address.sun_family = AF_UNIX;
  address.sun_path[0] = '\0';
  constexpr size_t name_length = sizeof(kControlSocketName) - 1;
  static_assert(name_length + 1 <= sizeof(address.sun_path));
  std::memcpy(address.sun_path + 1, kControlSocketName, name_length);
  const auto address_length = static_cast<socklen_t>(
      offsetof(sockaddr_un, sun_path) + 1 + name_length);
  if (bind(socket_fd, reinterpret_cast<const sockaddr*>(&address), address_length) < 0) {
    std::cerr << "Unable to bind display control socket: " << std::strerror(errno) << '\n';
    close(socket_fd);
    return -1;
  }
  return socket_fd;
}

void ApplyControlMessage(const std::string& message, PresentationControl* control,
                         std::chrono::steady_clock::time_point now =
                             std::chrono::steady_clock::now()) {
  using cube_display::CubeState;
  if (message == "idle") {
    SetPresentationState(CubeState::Idle, now, control);
    control->speech_level = 0;
  } else if (message == "wake" || message == "listening") {
    SetPresentationState(CubeState::Listening, now, control);
    control->speech_level = 0;
  } else if (message == "thinking") {
    SetPresentationState(CubeState::Thinking, now, control);
    control->speech_level = 0;
  } else if (message == "followup") {
    SetPresentationState(CubeState::Followup, now, control);
    control->speech_level = 0;
  } else {
    constexpr char speech_prefix[] = "speech ";
    if (message.rfind(speech_prefix, 0) != 0) return;
    const char* level_start = message.c_str() + sizeof(speech_prefix) - 1;
    char* level_end = nullptr;
    errno = 0;
    const double level = std::strtod(level_start, &level_end);
    if (level_end == level_start || *level_end != '\0' || errno == ERANGE ||
        !std::isfinite(level)) return;
    SetPresentationState(CubeState::Speaking, now, control);
    control->speech_level = std::clamp(level, 0.0, 1.0);
    control->last_voice_update = now;
  }
  control->last_state_update = now;
}

void AdvancePresentation(std::chrono::steady_clock::time_point now,
                         PresentationControl* control) {
  using cube_display::CubeState;
  if (control->state == CubeState::Speaking &&
      now - control->last_voice_update > std::chrono::milliseconds(180))
    control->speech_level = 0;
  if (control->state != CubeState::Idle &&
      now - control->last_state_update > std::chrono::seconds(15)) {
    SetPresentationState(CubeState::Idle, now, control);
    control->speech_level = 0;
  }
}

void ReadControlMessages(int socket_fd, uid_t allowed_uid, PresentationControl* control,
                         cube_content::ContentRenderer* content = nullptr,
                         bool content_available = false,
                         cube_display::WeatherState* weather = nullptr,
                         cube_display::MusicState* music = nullptr) {
  if (socket_fd < 0) return;
  constexpr size_t kMaximumMessagesPerFrame = 64;
  bool artwork_applied = false;
  for (size_t message_count = 0; message_count < kMaximumMessagesPerFrame;
       ++message_count) {
    std::array<char, cube_display::MusicState::kMaximumPacket> buffer{};
    char credential_buffer[CMSG_SPACE(sizeof(ucred))] = {};
    iovec io_vector{};
    io_vector.iov_base = buffer.data();
    io_vector.iov_len = buffer.size();
    sockaddr_un sender{};
    msghdr message_header{};
    message_header.msg_name = &sender;
    message_header.msg_namelen = sizeof(sender);
    message_header.msg_iov = &io_vector;
    message_header.msg_iovlen = 1;
    message_header.msg_control = credential_buffer;
    message_header.msg_controllen = sizeof(credential_buffer);

    const ssize_t bytes_read = recvmsg(socket_fd, &message_header, 0);
    if (bytes_read > 0) {
      bool authorized = false;
      for (cmsghdr* header = CMSG_FIRSTHDR(&message_header);
           header != nullptr; header = CMSG_NXTHDR(&message_header, header)) {
        if (header->cmsg_level == SOL_SOCKET && header->cmsg_type == SCM_CREDENTIALS &&
            header->cmsg_len >= CMSG_LEN(sizeof(ucred))) {
          const auto* credentials = reinterpret_cast<const ucred*>(CMSG_DATA(header));
          authorized = credentials->uid == allowed_uid || credentials->uid == 0;
          break;
        }
      }
      if (authorized && !(message_header.msg_flags & (MSG_TRUNC | MSG_CTRUNC))) {
        const std::string_view packet(buffer.data(), static_cast<size_t>(bytes_read));
        if (packet.substr(0, 10) == "music-art ") {
          if (music && !artwork_applied)
            artwork_applied = music->ApplyArtwork(packet, std::chrono::steady_clock::now());
          continue;
        }
        if (packet.substr(0, 6) == "music ") {
          if (music) music->Apply(packet, std::chrono::steady_clock::now());
          continue;
        }
        if (packet.size() > 128) continue;
        const std::string message(packet);
        if (message.rfind("control ", 0) == 0) {
          const bool accepted = control->display.Apply(message);
          const std::string reply = accepted ? control->display.Status() :
              (content ? cube_content::ApplyContentCommand(message, *content,
                                                             content_available) : "{}");
          if (message_header.msg_namelen > offsetof(sockaddr_un, sun_path)) {
            sendto(socket_fd, reply.data(), reply.size(), MSG_DONTWAIT,
                   reinterpret_cast<const sockaddr*>(&sender), message_header.msg_namelen);
          }
        } else if (message.rfind("weather ", 0) == 0) {
          if (weather) weather->Apply(message, std::chrono::steady_clock::now());
        } else ApplyControlMessage(message, control);
      }
      continue;
    }
    if (bytes_read < 0 && errno != EAGAIN && errno != EWOULDBLOCK)
      std::cerr << "Display control read failed: " << std::strerror(errno) << '\n';
    break;
  }
}

cube_display::DisplayApp* SelectApp(const cube_display::DisplayContext& context,
    bool content_suspended, cube_display::CarouselScheduler& carousel,
    const cube_display::MusicState& music_state, cube_display::MusicApp& music) {
  const bool music_notice = music_state.PresentationNotice(context);
  auto* selected = carousel.Update(context, content_suspended || music_notice);
  return music_notice ? &music : selected;
}

// Optional fixed-storage diagnostics: no per-frame logging or new worker.
struct FrameStats {
  bool enabled = false;
  unsigned frames = 0, overruns = 0;
  double max_setup_ms = 0, max_compose_ms = 0, max_work_ms = 0, max_interval_ms = 0;
  std::array<unsigned, 101> intervals{};
  void Record(double setup_ms, double compose_ms, double work_ms, double interval_ms) {
    if (!enabled) return;
    ++frames;
    if (work_ms > 33) ++overruns;
    max_setup_ms = std::max(max_setup_ms, setup_ms);
    max_compose_ms = std::max(max_compose_ms, compose_ms);
    max_work_ms = std::max(max_work_ms, work_ms);
    max_interval_ms = std::max(max_interval_ms, interval_ms);
    ++intervals[static_cast<size_t>(std::clamp(static_cast<int>(interval_ms), 0, 100))];
    if (frames < 300) return;
    unsigned cumulative = 0;
    size_t p95 = 0;
    for (; p95 < intervals.size(); ++p95) {
      cumulative += intervals[p95];
      if (cumulative * 100 >= frames * 95) break;
    }
    std::cerr << "renderer frames=" << frames << " overruns=" << overruns
              << " setup_max_ms=" << max_setup_ms << " compose_max_ms=" << max_compose_ms
              << " work_max_ms=" << max_work_ms
              << " interval_max_ms=" << max_interval_ms
              << " interval_p95_ms=" << p95 << '\n';
    frames = overruns = 0;
    max_setup_ms = max_compose_ms = max_work_ms = max_interval_ms = 0;
    intervals.fill(0);
  }
};

}  // namespace

int main(int argc, char* argv[]) {
  RGBMatrix::Options options;
  options.hardware_mapping = "regular";
  options.rows = cube_display::kHeight;
  options.cols = cube_display::kWidth;
  options.chain_length = 1;
  options.parallel = 1;
  options.brightness = 10;

  uid_t control_uid = 0;
  const int control_socket = CreateControlSocket(&control_uid);
  if (control_socket < 0) return 1;
  RGBMatrix* matrix = RGBMatrix::CreateFromFlags(&argc, &argv, &options);
  if (matrix == nullptr) {
    close(control_socket);
    return 1;
  }
  std::signal(SIGINT, HandleSignal);
  std::signal(SIGTERM, HandleSignal);

  FrameCanvas* frame = matrix->CreateFrameCanvas();
  const bool content_available = frame->width() == cube_display::kWidth &&
                                 frame->height() == cube_display::kHeight;
  constexpr auto kFrameTime = std::chrono::milliseconds(33);
  PresentationControl control;
  control.display.master_brightness_percent = options.brightness;
  // Sources and apps precede their non-owning registry/scheduler/runtime users.
  cube_display::SystemLocalClock local_clock;
  cube_display::TetrisClockApp tetris_clock(local_clock);
  cube_display::WeatherState weather_state;
  cube_display::WeatherApp weather(weather_state);
  cube_display::MusicState music_state;
  cube_display::MusicApp music(music_state);
  cube_display::AnimationCatalog animation_catalog;
  // The installed service runs from client/native/cube-display.
  animation_catalog.LoadDirectory("../../assets/animations");
  cube_display::AnimationApp animation(animation_catalog);
  cube_display::DisplayAppRegistry registry;
  registry.Register(cube_display::TetrisClockApp::kId, tetris_clock);
  registry.Register(cube_display::WeatherApp::kId, weather);
  registry.Register(cube_display::AnimationApp::kId, animation);
  registry.Register(cube_display::MusicApp::kId, music);
  cube_display::CarouselScheduler carousel(registry);
  std::string carousel_error;
  if (!carousel.Configure({{cube_display::TetrisClockApp::kId, std::chrono::seconds(15)},
                           {cube_display::WeatherApp::kId, std::chrono::seconds(15)},
                           {cube_display::AnimationApp::kId, std::chrono::seconds(10)},
                           {cube_display::MusicApp::kId, std::chrono::seconds(10)}},
                          &carousel_error))
    std::cerr << "Carousel configuration rejected: " << carousel_error << '\n';
  cube_display::DisplayRuntime runtime;
  cube_display::DisplayApp* installed_app = nullptr;
  cube_content::ContentRenderer content;
  FrameStats stats;
  const char* stats_setting = std::getenv("CUBE_RENDERER_STATS");
  stats.enabled = stats_setting && std::string(stats_setting) == "1";
  auto previous_frame = std::chrono::steady_clock::now();
  int applied_brightness = options.brightness;

  while (!g_stop) {
    const auto frame_started = std::chrono::steady_clock::now();
    const auto elapsed = frame_started - previous_frame;
    const double interval_ms = std::chrono::duration<double, std::milli>(elapsed).count();
    previous_frame = frame_started;
    ReadControlMessages(control_socket, control_uid, &control, &content, content_available,
                        &weather_state, &music_state);
    AdvancePresentation(frame_started, &control);

    const int master = control.display.master_brightness_percent;
    // The matrix library accepts 1..100. At zero, clear the submitted pixels.
    if (master > 0 && master != applied_brightness) {
      matrix->SetBrightness(static_cast<uint8_t>(master));
      applied_brightness = master;
    }
    double setup_ms = 0, compose_ms = 0;
    if (content_available) {
      const auto setup_started = std::chrono::steady_clock::now();
      const bool applied = content.ApplyPending(control.display.visible());
      if (applied) setup_ms = std::chrono::duration<double, std::milli>(
          std::chrono::steady_clock::now() - setup_started).count();
      const cube_display::DisplayContext context{frame_started, elapsed};
      auto* selected = SelectApp(context, content.state() != cube_content::Presentation::Full,
                                carousel, music_state, music);
      if (selected != installed_app) {
        runtime.SetActiveApp(selected);
        installed_app = selected;
      }
      const auto& base = runtime.Render(context);
      const double state_elapsed = std::chrono::duration<double>(
          frame_started - control.state_started_at).count();
      const cube_display::PresentationState presentation{
          control.state, control.speech_level, state_elapsed};
      const auto composed = cube_display::Compose(base, presentation);
      const auto compose_started = std::chrono::steady_clock::now();
      const auto& pixels = content.Compose(composed, interval_ms);
      compose_ms = std::chrono::duration<double, std::milli>(
          std::chrono::steady_clock::now() - compose_started).count();
      if (control.display.visible()) {
        for (int y = 0; y < cube_display::kHeight; ++y)
          for (int x = 0; x < cube_display::kWidth; ++x) {
            const auto color = pixels[cube_display::PixelIndex(x, y)];
            frame->SetPixel(x, y, color.r, color.g, color.b);
          }
      } else frame->Clear();
    } else frame->Clear();

    frame = matrix->SwapOnVSync(frame);
    if (content_available)
      content.Presented(control.display.visible());
    const auto work = std::chrono::steady_clock::now() - frame_started;
    stats.Record(setup_ms, compose_ms,
                 std::chrono::duration<double, std::milli>(work).count(), interval_ms);
    if (work < kFrameTime) std::this_thread::sleep_for(kFrameTime - work);
  }

  matrix->Clear();
  close(control_socket);
  // The matrix destructor can leave the still-powered panel displaying random
  // pixels after releasing GPIO/PIO. The OS reclaims it on process exit.
  return 0;
}
