#include "apps/music.h"
#include "carousel-scheduler.h"
#include "display-compositor.h"

#include <cassert>
#include <fstream>
#include <iterator>
#include <string>

using namespace cube_display;
using namespace std::chrono;

DisplayContext At(int ms) { return {steady_clock::time_point(milliseconds(ms)), milliseconds(33)}; }
const std::string track(64, 'a'), art(64, 'b'), zero(64, '0');
std::string State(int seq, int status, int progress = 25000, int expiry = 115000,
                  std::string id = track, int epoch = 10, int duration = 100000) {
  return "music v1 " + std::to_string(epoch) + " " + std::to_string(seq) + " " +
      std::to_string(expiry) + " " + std::to_string(status) + " " + id + " " +
      (status ? art : zero) + " " + std::to_string(duration) + " " + std::to_string(progress);
}
struct ClockApp : DisplayApp {
  bool Available(const DisplayContext&) const override { return true; }
  void Render(const DisplayContext&, Frame64* frame) override { frame->fill({1, 2, 3}); }
};
std::string Read(const std::string& path) {
  std::ifstream stream(path, std::ios::binary);
  return {std::istreambuf_iterator<char>(stream), std::istreambuf_iterator<char>()};
}

int main(int argc, char** argv) {
  assert(argc == 2);
  MusicState state;
  MusicApp music(state);
  Frame64 frame;
  assert(!music.Available(At(100000)));
  // Real Python encoder packets, not a second test-only wire implementation.
  assert(state.Apply(Read(std::string(argv[1]) + "/metadata"), At(100000).now));
  const auto image = Read(std::string(argv[1]) + "/artwork");
  assert(!state.ApplyArtwork(image.substr(0, image.size() - 1), At(100000).now));
  assert(state.ApplyArtwork(image, At(100000).now));
  assert(state.Progress(At(101000)) == 26000);
  music.Render(At(100000), &frame);
  assert((frame[PixelIndex(3, 0)] == Rgb{255, 0, 0}));
  assert((frame[PixelIndex(60, 57)] == Rgb{255, 0, 0}));
  // Artwork fills the top 58 rows, with a two-row gap before controls.
  for (int y = 0; y < 64; ++y)
    for (int x = 0; x < 64; ++x)
      if (x < 3 || x > 60 || y > 62 || y == 58 || y == 59)
        assert((frame[PixelIndex(x, y)] == Rgb{}));
  for (int x = 10; x <= 60; ++x)
    assert((frame[PixelIndex(x, 61)] == (x < 22 ? Rgb{200, 200, 200} : Rgb{25, 25, 25})));
  for (const auto name : {"square", "landscape", "portrait", "odd", "black"}) {
    assert(state.ApplyArtwork(Read(std::string(argv[1]) + "/" + name), At(100000).now));
    music.Render(At(100000), &frame);
    Frame64 expected{};
    const int width = std::string(name) == "portrait" ? 24 : 48;
    const int height = std::string(name) == "landscape" ? 24 : std::string(name) == "odd" ? 31 : 48;
    const int padding_x = (48 - width) / 2, padding_y = (48 - height) / 2;
    if (std::string(name) != "black")
      for (int y = 0; y < 58; ++y)
        for (int x = 0; x < 58; ++x) {
          const int source_x = (2 * x + 1) * 48 / 116 - padding_x;
          const int source_y = (2 * y + 1) * 48 / 116 - padding_y;
          if (source_x >= 0 && source_x < width && source_y >= 0 && source_y < height)
            PixelAt(expected, 3 + x, y) =
                {static_cast<uint8_t>(source_x + 1), static_cast<uint8_t>(source_y + 1), 200};
        }
    for (int y = 60; y <= 62; ++y) {
      PixelAt(expected, 3, y) = {200, 200, 200};
      PixelAt(expected, y == 61 ? 5 : 4, y) = {200, 200, 200};
    }
    for (int x = 10; x <= 60; ++x)
      PixelAt(expected, x, 61) = x < 22 ? Rgb{200, 200, 200} : Rgb{25, 25, 25};
    assert(frame == expected);
    if (std::string(name) == "square") {
      // All four artwork edges survive; the source frame padding is excluded.
      assert((frame[PixelIndex(3, 0)] == Rgb{1, 1, 200}));
      assert((frame[PixelIndex(60, 0)] == Rgb{48, 1, 200}));
      assert((frame[PixelIndex(3, 57)] == Rgb{1, 48, 200}));
      assert((frame[PixelIndex(60, 57)] == Rgb{48, 48, 200}));
    }
  }
  for (int x = 0; x < 64; ++x) assert((frame[PixelIndex(x, 63)] == Rgb{}));
  assert((frame[PixelIndex(0, 0)] == Rgb{}));
  const auto composed = Compose(frame, {CubeState::Listening, 0, 1});
  for (int y = 0; y < 63; ++y)
    for (int x = 0; x < 64; ++x) assert(composed[PixelIndex(x, y)] == frame[PixelIndex(x, y)]);
  // Seek correction, pause edge, repeated pause cannot extend the notice.
  assert(state.Apply(State(2, 1, 1000), At(101000).now));
  assert(!state.ApplyArtwork(image, At(101000).now));
  assert(state.Progress(At(102000)) == 2000);
  assert(state.Apply(State(3, 2, 2000), At(102000).now));
  music.Render(At(102000), &frame);
  for (int y = 60; y <= 62; ++y) {
    assert((frame[PixelIndex(3, y)] == Rgb{200, 200, 200}));
    assert((frame[PixelIndex(6, y)] == Rgb{200, 200, 200}));
    assert((frame[PixelIndex(4, y)] == Rgb{}));
    assert((frame[PixelIndex(5, y)] == Rgb{}));
  }
  assert(state.PauseNotice(At(102000)));
  assert(state.Progress(At(106000)) == 2000);
  assert(state.Apply(State(4, 2, 2000), At(106000).now));
  assert(state.PauseNotice(At(106999)));
  assert(!state.PauseNotice(At(107000)));
  assert(!state.Available(At(107000)));
  assert(state.Apply(State(5, 1, 2000), At(107000).now));
  assert(!state.PauseNotice(At(107000)));
  assert(state.Available(At(107000)));
  assert(state.Apply(State(6, 3, 3000), At(108000).now));
  assert(state.Progress(At(109000)) == 3000);
  assert(!state.Available(At(115000)));
  assert(!state.Apply(State(7, 1), At(115000).now)); // expired queued packet
  assert(!state.Apply(State(7, 1, 0, 140000), At(115000).now)); // excessive lease
  assert(!state.Apply(State(6, 1), At(110000).now)); // replay
  assert(!state.Apply(State(7, 1, 0, 115000, track, 9), At(110000).now)); // older provider
  assert(state.Apply(State(1, 2, 0, 115000, track, 11), At(110000).now));
  assert(!state.PauseNotice(At(110000))); // restart while paused is not a new edge
  assert(!state.has_artwork());
  assert(!state.ApplyArtwork(image, At(110000).now));
  assert(state.Apply(State(2, 1, -1, 115000, std::string(64, 'c'), 11, -1), At(110000).now));
  music.Render(At(110000), &frame);
  assert((frame[PixelIndex(27, 17)] == Rgb{100, 160, 200}));
  for (int x = 10; x <= 60; ++x) assert((frame[PixelIndex(x, 61)] == Rgb{}));
  assert(state.Apply(State(3, 0, -1, 0, zero, 11, -1), At(110000).now));
  assert(!state.Available(At(110000)));

  // Unknown/zero duration or unknown progress hides only the bar; endpoints
  // fill exactly zero/all 51 pixels. The indicator never moves.
  for (const int duration : {-1, 0, 100000})
    for (const int progress : {-1, 0, 100000}) {
      MusicState position;
      MusicApp app(position);
      assert(position.Apply(State(1, 1, progress, 115000, track, 10, duration), At(100000).now));
      app.Render(At(100000), &frame);
      assert((frame[PixelIndex(3, 60)] == Rgb{200, 200, 200}));
      for (int x = 10; x <= 60; ++x) {
        const Rgb expected = duration <= 0 || progress < 0 ? Rgb{} :
            progress == 0 ? Rgb{25, 25, 25} : Rgb{200, 200, 200};
        assert(frame[PixelIndex(x, 61)] == expected);
      }
      for (int x = 0; x < 64; ++x) assert((frame[PixelIndex(x, 63)] == Rgb{}));
    }

  // The carousel keeps the interrupted clock's remaining dwell.
  MusicState pause;
  MusicApp pause_app(pause);
  ClockApp clock;
  DisplayAppRegistry registry;
  assert(registry.Register("clock", clock));
  assert(registry.Register("music", pause_app));
  CarouselScheduler carousel(registry);
  assert(carousel.Configure({{"clock", seconds(15)}, {"music", seconds(10)}}));
  assert(pause.Apply(State(1, 1), At(100000).now));
  assert(carousel.Update(At(100000), false) == &clock);
  assert(pause.Apply(State(2, 2), At(104000).now));
  assert(pause.PauseNotice(At(104000)));
  assert(carousel.Update(At(104000), true) == &clock);
  assert(carousel.Update(At(108999), true) == &clock);
  assert(!pause.PauseNotice(At(109000)));
  assert(carousel.Update(At(109000), false) == &clock);
  assert(pause.Apply(State(3, 1, 0, 124000), At(109000).now));
  assert(carousel.Update(At(119999), false) == &clock);
  assert(carousel.Update(At(120000), false) == &pause_app);
  // Resume cancels an in-progress notice.
  assert(pause.Apply(State(4, 2, 0, 125000), At(120000).now));
  assert(pause.PauseNotice(At(120000)));
  assert(pause.Apply(State(5, 1, 0, 126000), At(121000).now));
  assert(!pause.PauseNotice(At(121000)));
  assert(pause.PresentationNotice(At(121000)));

  MusicState playing;
  assert(playing.Apply(State(1, 1), At(100000).now));
  assert(playing.PresentationNotice(At(100000)));
  assert(playing.Apply(State(2, 1), At(104000).now));
  assert(!playing.PresentationNotice(At(105000))); // repeated playing never extends it
  assert(playing.Apply(State(3, 3), At(105000).now));
  assert(playing.Apply(State(4, 1), At(106000).now));
  assert(!playing.PresentationNotice(At(106000))); // buffering recovery is not a new song
  assert(playing.Apply(State(5, 1, 0, 122000, std::string(64, 'c')), At(107000).now));
  assert(playing.PresentationNotice(At(107000))); // next track
  assert(!playing.PresentationNotice(At(112000)));
  assert(playing.Apply(State(6, 0, -1, 0, zero), At(108000).now));
  assert(!playing.PresentationNotice(At(108000)));
}
