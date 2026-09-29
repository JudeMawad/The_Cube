// Production translation unit with inert matrix facade and injected socket.
#include <sys/socket.h>
#include <sys/un.h>
#include <vector>
#include <string>
#include <cstring>
#include <cassert>
#include <cerrno>

struct Datagram {
  std::string text;
  uid_t uid = 1000;
  int flags = 0;
  bool credentials = true;
};
std::vector<Datagram> incoming;
std::vector<std::string> replies;
std::size_t consumed = 0;

ssize_t TestReceive(int, msghdr* message, int) {
  if (consumed == incoming.size()) { errno = EAGAIN; return -1; }
  const auto& datagram = incoming[consumed++];
  const auto size = std::min(datagram.text.size(), message->msg_iov->iov_len);
  std::memcpy(message->msg_iov->iov_base, datagram.text.data(), size);
  message->msg_flags = datagram.flags | (size < datagram.text.size() ? MSG_TRUNC : 0);
  message->msg_namelen = sizeof(sockaddr_un);
  if (datagram.credentials) {
    auto* header = CMSG_FIRSTHDR(message);
    header->cmsg_level = SOL_SOCKET;
    header->cmsg_type = SCM_CREDENTIALS;
    header->cmsg_len = CMSG_LEN(sizeof(ucred));
    ucred credential{};
    credential.uid = datagram.uid;
    std::memcpy(CMSG_DATA(header), &credential, sizeof(credential));
  } else message->msg_controllen = 0;
  return static_cast<ssize_t>(size);
}

ssize_t TestSend(int, const void* bytes, size_t size, int, const sockaddr*, socklen_t) {
  replies.emplace_back(static_cast<const char*>(bytes), size);
  return static_cast<ssize_t>(size);
}

#define recvmsg TestReceive
#define sendto TestSend
#define main UncalledRendererMain
#include "cube-display.cc"
#undef main
#undef recvmsg
#undef sendto

namespace {
void ReceiveBatch(PresentationControl& control, cube_content::ContentRenderer& content,
                  cube_display::WeatherState* weather = nullptr) {
  ReadControlMessages(123, 1000, &control, &content, true, weather);
}

void WeatherSocketProtocol() {
  PresentationControl control;
  cube_content::ContentRenderer content;
  cube_display::WeatherState weather;
  const auto now = std::chrono::steady_clock::now();
  const auto prior_replies = replies.size();
  incoming.push_back({"weather v1 -35 100 6 0 1800", 1001});
  incoming.push_back({"weather v1 -35 100 6 0 1800", 1000, MSG_TRUNC});
  incoming.push_back({"weather v1 -35 100 6 0 1800", 1000, 0, false});
  ReceiveBatch(control, content, &weather);
  assert(!weather.Available({now, std::chrono::milliseconds(33)}));
  incoming.push_back({"weather v1 -35 100 6 0 1800", 1000});
  ReceiveBatch(control, content, &weather);
  assert(weather.Available({now, std::chrono::milliseconds(33)}));
  assert(weather.snapshot().temperature_c10 == -35);
  incoming.push_back({"weather v2 500 100 6 0 1800", 1000});
  ReceiveBatch(control, content, &weather);
  assert(weather.snapshot().temperature_c10 == -35);
  assert(replies.size() == prior_replies); // weather messages do not request replies
}

void MusicSocketProtocol() {
  PresentationControl control;
  cube_content::ContentRenderer content;
  cube_display::MusicState music;
  const auto now = std::chrono::steady_clock::now();
  const auto expires = std::chrono::duration_cast<std::chrono::milliseconds>(
      now.time_since_epoch()).count() + 15000;
  const std::string track(64, 'a'), art(64, 'b');
  const auto state = "music v1 10 1 " + std::to_string(expires) + " 1 " + track + " " + art + " 100000 2000";
  const auto image = "music-art v1 10 1 " + track + " " + art + "\n" + std::string(12288, '\xff');
  const auto receive = [&] { ReadControlMessages(123, 1000, &control, &content, true, nullptr, &music); };
  for (const auto& text : {state, image}) {
    incoming.push_back({text, 1001});
    incoming.push_back({text, 1000, MSG_TRUNC});
    incoming.push_back({text, 1000, MSG_CTRUNC});
    incoming.push_back({text, 1000, 0, false});
  }
  receive();
  assert(!music.Available({now, {}}) && !music.has_artwork());
  incoming.push_back({state});
  incoming.push_back({image + "overflow"});
  receive();
  assert(music.Available({now, {}}) && !music.has_artwork());
  incoming.push_back({image});
  // At most one frame can be accepted in a renderer iteration.
  incoming.push_back({"music-art v1 10 1 " + track + " " + art + "\n" + std::string(12288, '\x01')});
  receive();
  assert(music.has_artwork());
  assert((music.artwork()[0] == cube_display::Rgb{255, 255, 255}));
  incoming.push_back({std::string(cube_display::MusicState::kMaximumPacket + 1, 'x')});
  receive();
  assert(music.has_artwork());
}

void MusicPresentation() {
  using namespace cube_display;
  using namespace std::chrono;
  struct Always : DisplayApp {
    bool Available(const DisplayContext&) const override { return true; }
    void Render(const DisplayContext&, Frame64* frame) override { frame->fill({}); }
  } clock;
  MusicState state;
  MusicApp music(state);
  DisplayAppRegistry registry;
  registry.Register("clock", clock);
  registry.Register("music", music);
  CarouselScheduler carousel(registry);
  assert(carousel.Configure({{"clock", seconds(15)}, {"music", seconds(10)}}));
  const auto at = [](int s) { return DisplayContext{steady_clock::time_point(seconds(s)), milliseconds(33)}; };
  const std::string ids = std::string(64, 'a') + " " + std::string(64, 'b') + " 100000 1000";
  assert(state.Apply("music v1 1 1 110000 1 " + ids, at(95).now));
  assert(SelectApp(at(95), false, carousel, state, music) == &music);
  assert(SelectApp(at(100), false, carousel, state, music) == &clock);
  assert(state.Apply("music v1 1 2 115000 2 " + ids, at(104).now));
  assert(SelectApp(at(104), false, carousel, state, music) == &music);
  assert(SelectApp(at(108), false, carousel, state, music) == &music);
  assert(SelectApp(at(109), false, carousel, state, music) == &clock);
  assert(state.Apply("music v1 1 3 124000 1 " + ids, at(109).now));
  assert(SelectApp(at(109), false, carousel, state, music) == &music);
  assert(SelectApp(at(114), false, carousel, state, music) == &clock);
  assert(state.Apply("music v1 1 4 129000 1 " + ids, at(114).now));
  assert(SelectApp(at(119), false, carousel, state, music) == &clock);
  assert(SelectApp(at(125), false, carousel, state, music) == &music);
  // Explicit content remains above the selected music frame; a hidden pause
  // expires on wall time, rather than replaying after the content disappears.
  assert(state.Apply("music v1 1 5 140000 2 " + ids, at(125).now));
  cube_content::ContentRenderer content;
  cube_content::ApplyContentCommand("control show_icon pause", content, true);
  content.ApplyPending(true);
  assert(SelectApp(at(126), true, carousel, state, music) == &music);
  Frame64 base;
  music.Render(at(126), &base);
  content.Compose(base, 1000);
  content.Presented(true);
  const auto with_music = content.Compose(base, 1000);
  Frame64 other;
  other.fill({1, 2, 3});
  const auto without_music = content.Compose(other, 1000);
  assert(with_music == without_music);
  assert(!state.PauseNotice(at(130)));
  assert(SelectApp(at(130), true, carousel, state, music) == &clock);
  PresentationControl control;
  control.display.Apply("control set_display off");
  assert(!control.display.visible());
  control.display.Apply("control set_display on");
  control.display.Apply("control set_brightness 0");
  assert(!control.display.visible());
}

void SocketProtocol() {
  PresentationControl control;
  cube_content::ContentRenderer content;
  incoming = {{"control show_text HI", 1001}, {"control show_text HI", 1000, MSG_CTRUNC},
              {"control show_text HI", 1000, 0, false},
              {"control show_text HI" + std::string(128, ' ')}};
  ReceiveBatch(control, content);
  assert(!content.pending() && replies.empty());
  incoming.push_back({"control show_text 23°"});
  ReceiveBatch(control, content);
  assert(content.pending() && replies.back() == "{\"accepted\":true}");
  incoming.push_back({"control get_status"});
  ReceiveBatch(control, content);
  assert(replies.back() == "{\"display_enabled\":true,\"master_brightness_percent\":10}");
  incoming.push_back({"control set_animation off"});
  ReceiveBatch(control, content);
  assert(replies.back() == "{}");
  incoming.push_back({"control get_content_status"});
  ReceiveBatch(control, content);
  assert(replies.back().find("\"pending\":true") != std::string::npos);
  const auto prior = content.Status();
  incoming.push_back({"control show_icon invalid"});
  incoming.push_back({std::string("control show_text HI\0!", 22)});
  ReceiveBatch(control, content);
  assert(replies.back() == "{}" && content.Status() == prior);
  incoming.push_back({"control set_brightness 50", 0});
  ReceiveBatch(control, content);
  assert(control.display.master_brightness_percent == 50);
  for (int i = 0; i < 70; ++i) incoming.push_back({"control show_text !"});
  const auto before = consumed;
  ReceiveBatch(control, content);
  assert(consumed - before == 64);
  ReceiveBatch(control, content);
  assert(consumed == incoming.size());
}

void PresentationAndBrightness() {
  PresentationControl control;
  const auto now = std::chrono::steady_clock::now();
  control.display.Apply("control set_brightness 50");
  ApplyControlMessage("thinking", &control, now);
  assert(control.state == cube_display::CubeState::Thinking);
  assert(control.display.master_brightness_percent == 50);
  ApplyControlMessage("speech 1", &control, now);
  assert(control.state == cube_display::CubeState::Speaking);
  assert(control.speech_level == 1);
  AdvancePresentation(now + std::chrono::milliseconds(181), &control);
  assert(control.speech_level == 0);
  AdvancePresentation(now + std::chrono::seconds(16), &control);
  assert(control.state == cube_display::CubeState::Idle);
  ApplyControlMessage("wake", &control, now);
  assert(control.state == cube_display::CubeState::Listening);
  assert(control.state_started_at == now);
  ApplyControlMessage("listening", &control, now + std::chrono::milliseconds(200));
  assert(control.state_started_at == now);  // Repeated state must not restart entrance.
  ApplyControlMessage("thinking", &control, now + std::chrono::milliseconds(300));
  assert(control.state_started_at == now + std::chrono::milliseconds(300));
  control.display.Apply("control set_brightness 0");
  assert(!control.display.visible());
  control.display.Apply("control set_brightness 100");
  assert(control.display.visible() && control.display.master_brightness_percent == 100);
}
}  // namespace

int main() {
  // Invalid account configuration must fail before opening the real socket or panel.
  uid_t allowed = 4321;
  unsetenv("CUBE_DISPLAY_USER");
  assert(CreateControlSocket(&allowed) == -1 && allowed == 4321);
  setenv("CUBE_DISPLAY_USER", "", 1);
  assert(CreateControlSocket(&allowed) == -1 && allowed == 4321);
  setenv("CUBE_DISPLAY_USER", "cube-test-user-that-must-not-exist-987654321", 1);
  assert(CreateControlSocket(&allowed) == -1 && allowed == 4321);
  unsetenv("CUBE_DISPLAY_USER");
  SocketProtocol(); WeatherSocketProtocol(); MusicSocketProtocol(); MusicPresentation(); PresentationAndBrightness();
  std::cout << "Production renderer regression assertions passed\n";
}
