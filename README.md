# ESP32 WiFi Sensing Dashboard

Detects movement in a room from changes in the WiFi signal between your router and a single ESP32 board, and shows it on a live local dashboard.

The board reads the WiFi signal details (CSI, or Channel State Information) of packets from your router. A small Python server reads the board over USB and streams the data to a web page, which works out motion level, activity and movement counts.

## What it shows

- **Room status**: "Movement detected", "All still" or "Not calibrated"
- **Events today**, last movement, signal strength and data rate
- **Motion level** chart with the calibrated threshold, plus hover readouts
- **What's happening**: a rough activity guess (walking, small movements, still), or one trained on your own recordings
- **Activity over time**: seconds of movement per minute over the last hour
- **Raw WiFi signal** heatmap, one row per frequency

## Hardware

- An original ESP32 dev board (tested with an ESP32-D0WD-V3 38-pin board with a CP2102 USB chip)
- A 2.4 GHz WiFi network. The ESP32 can't join 5 GHz.
- A Mac or Linux computer with a USB data cable

## Setup

### 1. Install ESP-IDF 5.4 or newer

```sh
brew install cmake ninja dfu-util
mkdir -p ~/esp && cd ~/esp
git clone --depth 1 -b v5.4.2 --recursive --shallow-submodules https://github.com/espressif/esp-idf.git esp-idf-v5.4
cd esp-idf-v5.4 && ./install.sh esp32
```

### 2. Build and flash Espressif's CSI firmware

```sh
cd ~/esp
git clone --depth 1 https://github.com/espressif/esp-csi.git
cd esp-csi
git apply /path/to/this/repo/firmware/cp2102-serial-speed.patch
cd examples/esp-radar/console_test
. ~/esp/esp-idf-v5.4/export.sh
idf.py set-target esp32
idf.py -p /dev/cu.usbserial-0001 flash
```

The patch lowers the serial speed from 2,000,000 to 921,600 baud. CP2102 USB chips can't keep up with 2,000,000, which garbles the data. Skip the patch if your board has a faster USB chip.

### 3. Run the dashboard

```sh
~/.espressif/python_env/idf5.4_py3.*_env/bin/python server.py --port /dev/cu.usbserial-0001
```

Open http://127.0.0.1:8765, then:

1. Enter your WiFi name and password and press **Connect**.
2. Press **Calibrate** and stay still, or leave the room, for 30 seconds.
3. Walk between the router and the ESP32 to test. Adjust **Sensitivity** if it's too jumpy or too quiet.

Any Python with `pyserial` installed works in place of the ESP-IDF environment.

### Disconnecting

- **Disconnect WiFi** takes the board off your network. The server won't reconnect it until you press **Connect** again.
- **USB port: Release** makes the server let go of the board, so you can unplug it or flash it with `idf.py` while the dashboard keeps running. **Reconnect** takes the port back. This restarts the board, which then rejoins WiFi if you connected it during this server session.

## Notes

- **Your WiFi password** goes only from the page to the board over USB. The server keeps it in memory only, to reconnect the board after a restart, and never writes it to disk. The server only accepts connections from your own computer.
- **Restarting the server restarts the board.** Opening the USB port resets ESP32 boards with a CP2102 chip, so the board drops off WiFi. While the server is running, it reconnects the board automatically.
- **Calibration, sensitivity, counts and activity recordings** are stored in your browser.

## How the motion detection works

The method follows [RuView](https://github.com/ruvnet/RuView):

1. A Hampel filter removes one-off spikes on each frequency.
2. Each frame is divided by its average, which cancels the chip's automatic gain jumps.
3. The motion level is the median, across frequencies, of the frame-to-frame change over about 1 second.

Calibration records 30 seconds of a still room and sets the threshold just above its natural noise. Movement is reported when at least 6 of the last 10 readings exceed it.

## Limits

- **Location:** one router and one board form a single signal path. The dashboard can tell that something moved near that path, but not where in the house, or who it was.
- **Range:** movement far from the router-to-board line, such as in another room, is often missed.
- **Activity:** walking vs. sitting vs. nobody works best. Standing still vs. an empty room is hard to tell apart.
- **More boards:** for room-level location or people counting, use 3 or more ESP32-S3 boards with RuView or a similar project.

## Files

- `server.py`: USB serial to web bridge (server-sent events)
- `index.html`: the dashboard
- `firmware/cp2102-serial-speed.patch`: serial speed fix for Espressif's `esp-csi` example
- `archive/`: earlier versions of the page (house map, 3D view)
