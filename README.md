# Local Video Deck

Compare, convert and AI-enhance videos on your own Linux computer, from your browser.
Nothing is uploaded: the app runs locally and only listens on `127.0.0.1`.

- **Compare** two versions of a video with a wipe slider, side by side, or by flipping between them.
  Both play in sync, step frame by frame, and zoom in together.
- **Convert** a video to a smaller size (4K, 1440p, 1080p, 720p or 480p) as H.264 or H.265.
- **Enhance** a video with [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN) to make it 2×, 3× or 4× bigger
  and sharper, with a model for real footage and a faster one for anime and cartoons.
- Every job shows its progress, the time left and, for Enhance, which step it's on.
  When it finishes, one click compares the new video with the original.
- You choose where files are saved, and the app remembers your folders and settings for next time.

## Install and run

```bash
git clone https://github.com/MortifisNox/Local-Video-Deck.git
cd Local-Video-Deck
./start.sh
```

`start.sh` sets everything up the first time, then opens the app in your browser. On later runs it starts straight away.

What it does the first time:

1. Checks for Python 3.8+, ffmpeg, the Vulkan loader and libgomp, and offers to install any that are missing
   with your package manager (`dnf`, `apt`, `pacman` or `zypper`). This asks for your password.
2. Downloads the official Real-ESRGAN Linux build (47 MB) into `tools/realesrgan`, and checks its SHA-256
   before installing it.
3. Starts the app at <http://127.0.0.1:8765> and opens it in your browser.

Keep the terminal window open while you use the app. Press <kbd>Ctrl</kbd>+<kbd>C</kbd> in it to stop.

### Options

| Command | What it does |
| --- | --- |
| `./start.sh --add-shortcut` | Adds Local Video Deck to your app menu |
| `./start.sh --install-only` | Installs what's missing without starting the app |
| `./start.sh --no-browser` | Starts without opening a browser tab |
| `./start.sh --port 9000` | Uses another port |
| `VIDEO_DECK_GPU=0 ./start.sh` | Makes Real-ESRGAN use a specific graphics card (Vulkan device number) |

## Requirements

- Linux on an x86_64 PC.
- A graphics card with a Vulkan driver for Enhance. NVIDIA, AMD and Intel all work. When there's more than one,
  the app picks a dedicated card over integrated graphics.
- Converting works on any computer. With an NVIDIA card and an ffmpeg build that has NVENC, it encodes on the
  graphics card; otherwise it uses `libx264` / `libx265` on the processor.
- Free disk space for Enhance. Frames are stored as images while a video is being enhanced, which can take tens
  of GB for a short clip. The app shows how much a video needs, next to how much is free, before you start.

## Using it

**Compare.** Drop the original on the left half and the enhanced video on the right half, or use the buttons.
<kbd>Space</kbd> plays and pauses, <kbd>←</kbd> <kbd>→</kbd> step one frame, <kbd>F</kbd> flips between versions,
scrolling zooms and dragging moves around. Set the frame rate to match your video so frame steps are exact.
This tab also works if you open `web/index.html` directly, without the helper.

**Convert.** Choose a video, a size, a format and a quality, then a folder. Sizes bigger than the original are
turned off. Use Enhance for those.

**Enhance.** Choose a video, what's in it, how much bigger to make it and a quality, then a folder.
Real footage uses `realesrgan-x4plus`: best detail, and slow. Anime and cartoons uses `realesr-animevideov3`,
which is much faster. If a job stops with a memory error, turn on **Use less graphics memory** and try again.

Jobs run one at a time, so they don't compete for the graphics card. New files never overwrite existing ones.
MP4 and MOV videos are saved as MP4. Other videos are saved as MKV so their subtitles and fonts are kept.

## Troubleshooting

- **Convert or Enhance says the helper isn't running.** You opened the page file directly. Run `./start.sh`
  and use the page it opens.
- **"Your ffmpeg can't encode H.264".** Fedora's `ffmpeg-free` package has limited encoders. Enable
  [RPM Fusion](https://rpmfusion.org/Configuration), then run `sudo dnf swap ffmpeg-free ffmpeg --allowerasing`.
- **"No graphics card with Vulkan support was found".** Install your graphics driver's Vulkan support
  (`mesa-vulkan-drivers` for AMD and Intel on Fedora; the proprietary driver for NVIDIA), then restart the app.
- **Enhance uses the wrong graphics card.** The terminal shows which card it chose when the app starts.
  Set `VIDEO_DECK_GPU` to the device number you want.
- **Port 8765 is in use.** Start with `./start.sh --port 9000`.

## Project layout

```
start.sh         installer and launcher
server.py        local helper: runs ffmpeg and Real-ESRGAN, serves the page (Python standard library only)
web/index.html   the app
web/icon.svg     app icon
tools/           Real-ESRGAN, downloaded by start.sh (not in git)
.work/           temporary frames while enhancing (not in git)
settings.json    your remembered folders and options (not in git)
```

## Credits

- [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN) by Xintao Wang et al., BSD-3-Clause.
  It's downloaded from its official releases page and isn't included in this repository.
- [ncnn](https://github.com/Tencent/ncnn) by Tencent, which the Real-ESRGAN portable build runs on.
- [FFmpeg](https://ffmpeg.org) for reading, converting and encoding video.
- [Archivo](https://fonts.google.com/specimen/Archivo) typeface by Omnibus-Type, SIL Open Font License.

## License

[MIT](LICENSE)
