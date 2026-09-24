// Qwen 生图：原生窗口外壳。启动 app.py 后台服务，用系统 WebKit 显示界面，关窗即退出。
import Cocoa
import WebKit

// 和 app.py 一样读 QWEN_PORT，默认 7861
let port = Int(ProcessInfo.processInfo.environment["QWEN_PORT"] ?? "") ?? 7861
let url = URL(string: "http://127.0.0.1:\(port)/")!
// .app 所在的目录就是工具根目录（app.py 在这里）
let root = Bundle.main.bundleURL.deletingLastPathComponent()

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKScriptMessageHandler, NSWindowDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var server: Process?

    func applicationDidFinishLaunching(_ note: Notification) {
        startServer()

        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "native")
        web = WKWebView(frame: .zero, configuration: config)
        web.uiDelegate = self

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Qwen 生图"
        window.minSize = NSSize(width: 760, height: 560)
        window.contentView = web
        window.delegate = self
        window.setFrameAutosaveName("QwenImageMain")
        if !window.setFrameUsingName("QwenImageMain") { window.center() }
        window.makeKeyAndOrderFront(nil)
        buildMenu()
        NSApp.activate(ignoringOtherApps: true)
        waitAndLoad(tries: 0)
    }

    func startServer() {
        let p = Process()
        let py = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
            .first { FileManager.default.isExecutableFile(atPath: $0) } ?? "/usr/bin/python3"
        p.executableURL = URL(fileURLWithPath: py)
        p.arguments = [root.appendingPathComponent("app.py").path]
        p.currentDirectoryURL = root
        var env = ProcessInfo.processInfo.environment
        env["QWEN_NO_BROWSER"] = "1"
        p.environment = env
        let logURL = root.appendingPathComponent("app.log")
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        if let log = try? FileHandle(forWritingTo: logURL) {
            log.seekToEndOfFile()
            p.standardOutput = log
            p.standardError = log
        }
        try? p.run()
        server = p
    }

    // 等服务起来再加载页面（已有实例在跑时也能直接连上）
    func waitAndLoad(tries: Int) {
        var req = URLRequest(url: url.appendingPathComponent("api/status"))
        req.timeoutInterval = 1
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            DispatchQueue.main.async {
                if (resp as? HTTPURLResponse)?.statusCode == 200 {
                    self.web.load(URLRequest(url: url))
                } else if tries < 240 {
                    // 首次运行时系统会先弹“访问下载文件夹”授权，授权前服务起不来，这里自动重试
                    if tries % 8 == 7, !(self.server?.isRunning ?? false) { self.startServer() }
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.waitAndLoad(tries: tries + 1) }
                } else {
                    self.web.loadHTMLString("<p style='font:15px -apple-system;padding:40px'>后台服务没能启动，请查看 app.log。</p>", baseURL: nil)
                }
            }
        }.resume()
    }

    func stopServer() {
        var req = URLRequest(url: url.appendingPathComponent("api/quit"))
        req.httpMethod = "POST"
        req.httpBody = "{}".data(using: .utf8)
        req.timeoutInterval = 2
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { _, _, _ in sem.signal() }.resume()
        _ = sem.wait(timeout: .now() + 2)
        if let p = server, p.isRunning {
            p.waitUntilExit(timeout: 3)
            if p.isRunning { p.terminate() }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ note: Notification) { stopServer() }

    // 页面里的“退出”按钮
    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        if message.body as? String == "quit" { NSApp.terminate(nil) }
    }

    // <input type=file> 选择参考图
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.image]
        panel.beginSheetModal(for: window) { completionHandler($0 == .OK ? panel.urls : nil) }
    }

    // JS 的 alert / confirm
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert(); a.messageText = message
        a.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert(); a.messageText = message
        a.addButton(withTitle: "确定"); a.addButton(withTitle: "取消")
        a.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "退出 Qwen 生图", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "编辑")
        edit.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let view = NSMenu(title: "显示")
        view.addItem(withTitle: "重新载入", action: #selector(reload), keyEquivalent: "r")
        viewItem.submenu = view
        NSApp.mainMenu = main
    }

    @objc func reload() { web.reload() }
}

extension Process {
    func waitUntilExit(timeout: TimeInterval) {
        let end = Date().addingTimeInterval(timeout)
        while isRunning && Date() < end { Thread.sleep(forTimeInterval: 0.1) }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
