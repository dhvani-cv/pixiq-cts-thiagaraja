# pixIQ Yarn Cone Inspection System

## Technical Documentation

**Version:** 1.0.0
**Date:** April 2026
**Platform:** Jetson Orin NX 16GB (pixIQ) + Basler GigE cameras

---

## Table of Contents

### Part I: System Overview
1. [System Overview](01_system_overview.md) — Purpose, hardware, software, inspection checks, result codes

### Part II: Architecture & Communication
2. [Architecture](02_architecture.md) — Services, components, data flow, thread model
3. [PLC Communication](03_plc_communication.md) — Modbus TCP handshake, register map, cycle flow
4. [Camera & Capture](04_camera_capture.md) — Basler pypylon, 3-camera sequential capture, hardware triggers, observability

### Part III: Inspection Pipeline
5. [Inspection Pipeline](05_inspection_pipeline.md) — End-to-end: YOLO → Dimensions → Stain → Pattern → UV → Tail
6. [Tube Pattern Matching](06_tube_pattern.md) — Color NN + FFT NN classification, teaching
7. [Stain Detection](07_stain_detection.md) — PatchCore anomaly detection on yarn surface
8. [UV Inspection](08_uv_inspection.md) — Radial dip thread mixup detection (physics-based)
9. [Tail Inspection](09_tail_inspection.md) — YOLO yarn tail presence detection

### Part IV: Services & API
10. [REST API](10_rest_api.md) — Teaching API (FastAPI, port 5002)
11. [Socket.IO Service](11_socketio_service.md) — Inspection service (eventlet, port 5004)
12. [Teaching Guide](12_teaching.md) — Operator reference for all 5 teaching modules

### Part V: Operations
13. [Configuration](13_configuration.md) — config.json schema, per-section reference
14. [Deployment](14_deployment.md) — Installation, services, startup, directory layout
15. [Logging & Monitoring](15_logging.md) — Log files, formats, Azure Monitor integration
16. [Testing](16_testing.md) — Unit tests, integration tests, local mock testing

### Part VI: pixIQ Platform
17. [pixIQ Setup](17_pixiq_setup.md) — First-time hardware setup, dual NIC, jumbo frames, pylon SDK, TensorRT export

### Appendices
- [Changelog](CHANGELOG.md) — Version history and breaking changes
- [PLC Update Notes](plc-update.md) — Pending PLC communication improvements

### Diagrams
All Mermaid diagrams are embedded inline in their respective chapters. Standalone `.mmd` files in `docs/diagrams/`:
- [pixiq_network.mmd](diagrams/pixiq_network.mmd) — Full styled network architecture
- [system_block_diagram.mmd](diagrams/system_block_diagram.mmd) — End-to-end system block diagram
- [inspection_sequence.mmd](diagrams/inspection_sequence.mmd) — Capture + inspection sequence

Inline Mermaid diagrams in chapters:
- System block diagram + conveyor layout + data flow → [Chapter 1](01_system_overview.md)
- Service architecture → [Chapter 2](02_architecture.md)
- PLC handshake, register map, error flows → [Chapter 3](03_plc_communication.md)
- Trigger-to-frame pipeline + capture sequence → [Chapter 4](04_camera_capture.md)
- Inspection pipeline flow + normal/trial mode sequences → [Chapter 5](05_inspection_pipeline.md)
- Tube pattern algorithm → [Chapter 6](06_tube_pattern.md)
- Stain detection pipeline → [Chapter 7](07_stain_detection.md)
- Socket.IO architecture → [Chapter 11](11_socketio_service.md)
- Network topology → [Chapter 14](14_deployment.md) + [Chapter 17](17_pixiq_setup.md)
- Inspection sequence + network topology → [project_context.md](project_context.md)

---

## Quick Reference

### Service Ports

| Service | Port | Protocol |
|---------|------|----------|
| Teaching API | 5002 | HTTP/REST (FastAPI) |
| Inspection Service | 5004 | WebSocket (Socket.IO) |
| Report Service (HMI) | 5001 | HTTP |
| PLC | 502 | Modbus TCP |

### Result Codes (PLC reg 40003)

| Code | Meaning |
|------|---------|
| 1 | Good — all checks passed |
| 2 | Defect — one or more checks failed |
| 3 | Error — system error (camera timeout, model failure) |

### Defect Type Codes (PLC reg 40020)

| Code | Defect |
|------|--------|
| 0 | Good |
| 1 | Stain |
| 2 | Wrong Pattern |
| 3 | Wrong Cone Diameter |
| 4 | Wrong Tube Diameter |
| 5 | Missing Tail |
| 6 | Thread Mixup |
| 7 | No Material ID |

### Key Directories

| Path | Purpose |
|------|---------|
| `src/` | Python source code |
| `data/recipes/` | JSON recipe files (material specs) |
| `data/templates/tube/` | .npz tube pattern reference files |
| `weights/` | YOLO model weights (VL, UV, Tail) |
| `models/` | PatchCore model (stain detection) |
| `logs/` | Log files (JSON + text, rotated) |
