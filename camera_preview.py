#!/usr/bin/env python3
"""List Linux V4L2 cameras and preview their video in a browser."""

import argparse
import glob
import html
import json
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import cv2


def cameras():
    """Return capture devices, excluding V4L2 metadata-only nodes."""
    found = []
    for path in sorted(glob.glob('/dev/video*')):
        try:
            result = subprocess.run(
                ['v4l2-ctl', '-d', path, '--info'], capture_output=True,
                text=True, timeout=3, check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
        info = result.stdout
        device_caps = re.search(r'Device Caps\s*:\s*0x[0-9a-fA-F]+\n((?:\s+[^\n]+\n)*)', info)
        if not device_caps or not re.search(r'\bVideo Capture\b', device_caps.group(1)):
            continue
        name = re.search(r'^\s*Card type\s*:\s*(.+)$', info, re.MULTILINE)
        bus = re.search(r'^\s*Bus info\s*:\s*(.+)$', info, re.MULTILINE)
        found.append({
            'device': path,
            'name': name.group(1).strip() if name else Path(path).name,
            'bus': bus.group(1).strip() if bus else '',
        })
    return found


def open_camera(path):
    deadline = time.monotonic() + 5
    while True:
        capture = cv2.VideoCapture(path, cv2.CAP_V4L2)
        if capture.isOpened():
            break
        capture.release()
        if time.monotonic() >= deadline:
            raise RuntimeError(f'Cannot open {path}; camera may be in use')
        time.sleep(0.25)
    capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    for _ in range(10):
        ok, frame = capture.read()
        if ok and frame is not None:
            return capture, frame
    capture.release()
    raise RuntimeError(f'Cannot read a frame from {path}')


def jpeg(frame):
    ok, encoded = cv2.imencode('.jpg', frame)
    if not ok:
        raise RuntimeError('JPEG encoding failed')
    return encoded.tobytes()


class Handler(BaseHTTPRequestHandler):
    def send_bytes(self, status, data, content_type):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        request = urlparse(self.path)
        if request.path == '/devices':
            return self.send_bytes(200, json.dumps(cameras(), ensure_ascii=False).encode(), 'application/json; charset=utf-8')
        if request.path == '/':
            cards = []
            for camera in cameras():
                path = camera['device']
                src = '/stream?device=' + quote(path, safe='')
                cards.append(
                    '<section><h2>' + html.escape(camera['name']) + '</h2>'
                    '<p>' + html.escape(path) + ' · ' + html.escape(camera['bus']) + '</p>'
                    '<img src="' + src + '" alt="Live video from ' + html.escape(path) + '"></section>'
                )
            if not cards:
                cards.append('<p>No video capture devices found. Check the camera connection and permissions.</p>')
            page = ('<!doctype html><html lang="en"><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<title>Camera preview</title><style>'
                    'body{font:16px system-ui,sans-serif;background:#111;color:#eee;max-width:1200px;margin:2rem auto;padding:0 1rem}'
                    'main{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:1rem}'
                    'section{background:#222;padding:1rem;border-radius:12px}h2{margin:.2rem 0}p{color:#bbb}'
                    'img{width:100%;height:auto;background:#000}a{color:#8cf}</style>'
                    '<h1>Camera preview</h1><p><a href="/">Refresh devices</a> · <a href="/devices">Device JSON</a></p>'
                    '<main>' + ''.join(cards) + '</main></html>')
            return self.send_bytes(200, page.encode(), 'text/html; charset=utf-8')
        if request.path not in ('/snapshot', '/stream'):
            return self.send_bytes(404, b'Not found\n', 'text/plain')
        path = parse_qs(request.query).get('device', [''])[0]
        if path not in {camera['device'] for camera in cameras()}:
            return self.send_bytes(400, b'Unknown capture device\n', 'text/plain')
        try:
            capture, frame = open_camera(path)
        except RuntimeError as error:
            return self.send_bytes(503, (str(error) + '\n').encode(), 'text/plain')
        try:
            if request.path == '/snapshot':
                return self.send_bytes(200, jpeg(frame), 'image/jpeg')
            self.send_response(200)
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            failures = 0
            while True:
                data = jpeg(frame)
                self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ' + str(len(data)).encode() + b'\r\n\r\n')
                self.wfile.write(data + b'\r\n')
                ok, frame = capture.read()
                if not ok or frame is None:
                    failures += 1
                    if failures >= 10:
                        break
                    continue
                failures = 0
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            capture.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    listing = sub.add_parser('list', help='List video capture devices')
    listing.add_argument('--check', action='store_true', help='Read a test frame from each device')
    listing.add_argument('--json', action='store_true', help='Print JSON')
    serving = sub.add_parser('serve', help='Start the browser preview')
    serving.add_argument('--host', default='127.0.0.1')
    serving.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if args.command == 'list':
        devices = cameras()
        if args.check:
            for camera in devices:
                try:
                    capture, frame = open_camera(camera['device'])
                    camera['status'] = 'ok'
                    camera['resolution'] = f'{frame.shape[1]}x{frame.shape[0]}'
                    capture.release()
                except RuntimeError as error:
                    camera['status'] = str(error)
        if args.json:
            print(json.dumps(devices, indent=2, ensure_ascii=False))
        else:
            for camera in devices:
                print(f"{camera['device']}: {camera['name']} ({camera['bus']})" +
                      (f" — {camera['status']} {camera.get('resolution', '')}" if args.check else ''))
            if not devices:
                print('No video capture devices found.')
        return 0 if devices and all(camera.get('status', 'ok') == 'ok' for camera in devices) else 1
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f'Camera preview: http://{args.host}:{args.port}/', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
