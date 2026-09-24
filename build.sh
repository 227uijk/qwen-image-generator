#!/bin/bash
# 构建「Qwen 生图.app」：编译原生窗口外壳、生成图标、打包并做本地签名。
# 用法：./build.sh    （需要 Xcode 命令行工具：xcode-select --install）
set -euo pipefail
cd "$(dirname "$0")"

APP="Qwen 生图.app"
BUILD="launcher/.build"
mkdir -p "$BUILD"

echo "→ 编译窗口外壳"
swiftc -O -o "$BUILD/QwenImage" launcher/main.swift

echo "→ 生成图标"
[ -f launcher/icon_1024.png ] || swift launcher/icon.swift launcher/icon_1024.png
ICONSET="$BUILD/AppIcon.iconset"
rm -rf "$ICONSET" && mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s launcher/icon_1024.png --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) launcher/icon_1024.png --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$BUILD/AppIcon.icns"

echo "→ 打包 $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BUILD/QwenImage" "$APP/Contents/MacOS/QwenImage"
cp "$BUILD/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>Qwen 生图</string>
<key>CFBundleDisplayName</key><string>Qwen 生图</string>
<key>CFBundleIdentifier</key><string>io.github.qwen-image-generator</string>
<key>CFBundleExecutable</key><string>QwenImage</string>
<key>CFBundleIconFile</key><string>AppIcon</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>LSMinimumSystemVersion</key><string>13.0</string>
<key>NSHighResolutionCapable</key><true/>
<key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST

codesign --force -s - "$APP"
echo "✓ 完成：双击「$APP」启动（它会在同目录下找 app.py，整个文件夹要放在一起）"
