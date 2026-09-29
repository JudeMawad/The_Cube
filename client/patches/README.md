# Matrix dependency patch

`rp1-pio-stop-dma-before-sm.patch` modifies `lib/rp1/rp1_pio_backend.cc` in [rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix) at commit `d7eef803089d49606c9a22170cbd8bd6c97dc2a3`.

The modification cancels queued DMA transfers before disabling the RP1 PIO state machine, preserving shutdown ordering. It is supplied under the upstream **GPL-2.0-or-later** terms; see [COPYING](COPYING). Upstream author/copyright notices remain in the patched source. Cube's root MIT license does not replace those terms.

`client/native/cube-display/prepare-matrix.py` exports the pinned dependency into an ignored build directory, applies this patch and verifies its reverse dry-run before building. It never modifies the submodule checkout. No compiled binary is included in this source release.
