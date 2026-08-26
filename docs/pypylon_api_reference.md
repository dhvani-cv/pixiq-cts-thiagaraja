# pypylon API Reference for GigE Hardware-Triggered Acquisition

Extracted from pypylon repo samples and Basler documentation. Target cameras:
- `acA1920-40gc` (ace GigE, UV station)
- `a2A1920-40gc` (ace 2 GigE, Tail station)
- `a2A2600-20gcPRO` (ace 2 GigE PRO, VL station)

---

## Installation

```bash
pip install pypylon
```

Requires pylon Camera Software Suite installed on the system.

---

## 1. Device Enumeration and Opening by IP

```python
from pypylon import pylon

tl_factory = pylon.TlFactory.GetInstance()

# List all devices
devices = tl_factory.EnumerateDevices()
for d in devices:
    print(d.GetFriendlyName(), d.GetIpAddress())

# Open by IP address
target_ip = "192.168.1.10"
for d in devices:
    if d.GetIpAddress() == target_ip:
        camera = pylon.InstantCamera(tl_factory.CreateDevice(d))
        break

# Alternative: create device info filter
di = pylon.DeviceInfo()
di.SetIpAddress(target_ip)
camera = pylon.InstantCamera(tl_factory.CreateFirstDevice(di))

camera.Open()
```

`DeviceInfo` filter methods: `SetIpAddress()`, `SetSerialNumber()`, `SetFriendlyName()`, `SetModelName()`.

---

## 2. Hardware Trigger Configuration (Line1, RisingEdge, ExposureStart)

```python
camera.Open()

# Set trigger mode
camera.TriggerMode.Value = "On"
camera.TriggerSource.Value = "Line1"          # Hardware input line
camera.TriggerActivation.Value = "RisingEdge" # or "FallingEdge"
camera.TriggerSelector.Value = "FrameStart"   # or "ExposureStart" if supported

# Debouncer (optional, reduce noise on trigger line)
camera.LineDebouncerTime.Value = 10.0  # microseconds
```

Node names follow the GenICam SFNC standard. Check availability:
```python
from pypylon import genicam
if genicam.IsAvailable(camera.TriggerActivation):
    camera.TriggerActivation.Value = "RisingEdge"
```

---

## 3. Exposure Time

```python
# Set exposure time in microseconds
camera.ExposureMode.Value = "Timed"
camera.ExposureTime.Value = 5000.0  # 5 ms

# Older cameras may use ExposureTimeAbs (ace classic)
# camera.ExposureTimeAbs.Value = 5000.0

# Query limits
print(camera.ExposureTime.Min, camera.ExposureTime.Max)
```

---

## 4. Grab Frames with Timeout

### Single frame
```python
# GrabOne: open, grab, close in one call (timeout in ms)
result = camera.GrabOne(5000)  # 5 second timeout
img = result.Array
result.Release()
```

### Continuous grab
```python
camera.StartGrabbingMax(100)  # grab N frames then stop
# or
camera.StartGrabbing(pylon.GrabStrategy_OneByOne)  # continuous until StopGrabbing()

while camera.IsGrabbing():
    grab_result = camera.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
    
    if grab_result.GrabSucceeded():
        img = grab_result.Array  # numpy array
        # process img
    else:
        print("Grab failed:", grab_result.ErrorCode, grab_result.ErrorDescription)
    
    grab_result.Release()  # MUST release to return buffer to pool

camera.StopGrabbing()
```

### Timeout handling options
```python
pylon.TimeoutHandling_ThrowException  # raises GenericException on timeout
pylon.TimeoutHandling_Return          # returns with grab_result.GrabSucceeded() == False
```

---

## 5. Convert Grabbed Images to NumPy BGR

### Direct array access (mono or Bayer)
```python
img = grab_result.Array  # numpy ndarray, shape depends on pixel format
# Mono8  -> (H, W) uint8
# BayerBG8 -> (H, W) uint8 (raw bayer, NOT debayered)
```

### ImageFormatConverter for BGR8 (OpenCV-compatible)
```python
converter = pylon.ImageFormatConverter()
converter.OutputPixelFormat = pylon.PixelType_BGR8packed
converter.OutputBitAlignment = pylon.OutputBitAlignment_MsbAligned

# Inside grab loop:
if grab_result.GrabSucceeded():
    image = converter.Convert(grab_result)
    img_bgr = image.GetArray()  # numpy (H, W, 3) uint8 BGR
```

### Check if conversion needed
```python
if not converter.ImageHasDestinationFormat(grab_result):
    image = converter.Convert(grab_result)
    img = image.GetArray()
else:
    img = grab_result.Array
```

### Pixel format options
```python
pylon.PixelType_BGR8packed    # OpenCV default
pylon.PixelType_RGB8packed    # RGB order
pylon.PixelType_Mono8         # Grayscale
```

---

## 6. Buffer Allocation and Management

```python
# Set number of grab buffers (default: 10)
camera.MaxNumBuffer.Value = 5

# Buffers are automatically allocated on StartGrabbing()
# and freed on StopGrabbing()

# CRITICAL: always call Release() on grab results to return buffer to pool
grab_result.Release()
```

### Grab strategies
```python
pylon.GrabStrategy_OneByOne        # FIFO - process every frame in order (default)
pylon.GrabStrategy_LatestImageOnly # Drop old frames, always get latest
pylon.GrabStrategy_LatestImages    # Keep N latest in output queue

# LatestImageOnly is best for triggered acquisition where you want the freshest frame
camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
```

---

## 7. GigE-Specific Settings

```python
camera.Open()

# Auto-negotiate optimal packet size (RECOMMENDED - call after Open)
camera.GevSCPSPacketSize.Value = camera.GevSCPSPacketSize.Max
# or use auto-detection:
camera.GevStreamChannelSelector.Value = 0

# Inter-packet delay (ticks) - spread packets to avoid NIC overflow
# Higher value = lower bandwidth but more reliable
camera.GevSCPD.Value = 1000  # inter-packet delay in ticks

# Frame transmission delay
camera.GevSCFTD.Value = 0

# Heartbeat timeout (ms) - how long before camera considers host dead
camera.GevHeartbeatTimeout.Value = 5000
# Disable heartbeat during debugging (camera won't timeout):
# camera.GevHeartbeatTimeout.Value = 60000000  # ~16 hours

# Transport layer parameters (via stream grabber node map)
stream_grabber = camera.GetStreamGrabberNodeMap()

# Packet resend settings
stream_grabber["MaxRetryCountRead"].Value = 3
stream_grabber["ReceiveTimeout"].Value = 1000

# Socket buffer size
stream_grabber["SocketBufferSize"].Value = 16 * 1024 * 1024  # 16 MB

# Enable packet resend
stream_grabber["EnableResend"].Value = True
```

---

## 8. Flush / Drain Buffers

```python
# Method 1: Stop and restart grabbing (full flush)
camera.StopGrabbing()
camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)

# Method 2: Drain pending results with short timeout
while True:
    grab_result = camera.RetrieveResult(0, pylon.TimeoutHandling_Return)
    if not grab_result.GrabSucceeded():
        break
    grab_result.Release()

# Method 3: Use LatestImageOnly strategy (auto-discards old frames)
camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
```

---

## 9. Stream Statistics

```python
stream_grabber = camera.GetStreamGrabberNodeMap()

# GigE Pylon stream grabber statistics (names may vary by pylon version)
stats = {
    "missed_frames": stream_grabber["Statistic_Total_Missed_Frame_Count"].Value,
    "failed_frames": stream_grabber["Statistic_Failed_Frame_Count"].Value,
    "buffer_underruns": stream_grabber["Statistic_Buffer_Underrun_Count"].Value,
    "resend_requests": stream_grabber["Statistic_Resend_Request_Count"].Value,
    "resend_packets": stream_grabber["Statistic_Resend_Packet_Count"].Value,
}

# Per-grab-result info
grab_result.GetBlockID()       # frame counter from camera
grab_result.GetTimeStamp()     # camera timestamp
grab_result.GetImageSize()     # bytes
grab_result.GetPayloadSize()   # total payload bytes
```

---

## 10. Software Trigger

### Manual software trigger
```python
camera.TriggerMode.Value = "On"
camera.TriggerSource.Value = "Software"

camera.StartGrabbing(pylon.GrabStrategy_OneByOne)

# Fire trigger
if camera.WaitForFrameTriggerReady(1000, pylon.TimeoutHandling_ThrowException):
    camera.ExecuteSoftwareTrigger()

grab_result = camera.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
```

### Using built-in SoftwareTriggerConfiguration
```python
# Replaces all existing configuration handlers
camera.RegisterConfiguration(
    pylon.SoftwareTriggerConfiguration(),
    pylon.RegistrationMode_ReplaceAll,
    pylon.Cleanup_Delete
)
camera.StartGrabbing(pylon.GrabStrategy_OneByOne)

# Trigger and grab
camera.WaitForFrameTriggerReady(100, pylon.TimeoutHandling_ThrowException)
camera.ExecuteSoftwareTrigger()
result = camera.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
```

---

## 11. Start / Stop Acquisition

```python
# Open connection
camera.Open()

# Start continuous acquisition
camera.StartGrabbing(pylon.GrabStrategy_OneByOne)

# Start with internal grab loop thread (results delivered via event handlers)
camera.StartGrabbing(pylon.GrabStrategy_OneByOne, pylon.GrabLoop_ProvidedByInstantCamera)

# Start and auto-stop after N frames
camera.StartGrabbingMax(100)

# Stop
camera.StopGrabbing()

# Close connection
camera.Close()

# Check state
camera.IsGrabbing()  # bool
camera.IsOpen()      # bool
```

---

## 12. Image Event Handlers (Background Grab Thread)

```python
class MyImageHandler(pylon.ImageEventHandler):
    def OnImageGrabbed(self, camera, grab_result):
        if grab_result.GrabSucceeded():
            img = grab_result.Array
            # process in callback thread

camera.RegisterImageEventHandler(
    MyImageHandler(),
    pylon.RegistrationMode_Append,
    pylon.Cleanup_Delete
)

# Use with internal grab loop
camera.StartGrabbing(
    pylon.GrabStrategy_OneByOne,
    pylon.GrabLoop_ProvidedByInstantCamera
)
```

---

## 13. Common Pixel Formats for These Cameras

| Camera | Default Format | Recommended |
|--------|---------------|-------------|
| acA1920-40gc | Mono8 or BayerBG8 | Mono8 (UV), BayerBG8 -> BGR8 via converter |
| a2A1920-40gc | BayerBG8 | BayerBG8 -> BGR8 via converter (Tail) |
| a2A2600-20gcPRO | BayerRG8 | BayerRG8 -> BGR8 via converter |

```python
camera.PixelFormat.Value = "Mono8"
# or
camera.PixelFormat.Value = "BayerBG8"
# or
camera.PixelFormat.Value = "BayerRG8"

# List available formats
print(camera.PixelFormat.Symbolics)
```

---

## 14. Error Handling

```python
from pypylon import genicam

try:
    camera.Open()
except genicam.GenericException as e:
    print(f"pylon error: {e}")
except genicam.TimeoutException as e:
    print(f"Timeout: {e}")
except genicam.RuntimeException as e:
    print(f"Runtime error: {e}")
```

---

## 15. Feature Availability Check

```python
from pypylon import genicam

# Check if a node exists and is available
if genicam.IsAvailable(camera.TriggerActivation):
    camera.TriggerActivation.Value = "RisingEdge"

if genicam.IsWritable(camera.ExposureTime):
    camera.ExposureTime.Value = 5000.0

if genicam.IsReadable(camera.ResultingFrameRate):
    print(f"FPS: {camera.ResultingFrameRate.Value}")
```

---

## Quick Reference: Import Pattern

```python
from pypylon import pylon      # main module: InstantCamera, TlFactory, GrabStrategy, etc.
from pypylon import genicam    # GenICam exceptions and node access utilities
import numpy as np
import cv2
```
