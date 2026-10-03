import Cocoa
import WebKit

final class Studio: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var child: Process?
    var baseURL: URL?
    var ready: URL!
    var dataURL: URL!
    var logHandle: FileHandle?
    var startupTimer: Timer?
    var attempts = 0
    var terminating = false
    var smokeDone = false
    let smokePath = ProcessInfo.processInfo.environment["NOON_SMOKE_OUTPUT"]
    var downloadTargets: [ObjectIdentifier: URL] = [:]

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        buildMenu()
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsBackForwardNavigationGestures = false
        window = NSWindow(contentRect: NSRect(x: 0,y: 0,width: 1320,height: 860),styleMask: [.titled,.closable,.miniaturizable,.resizable],backing: .buffered,defer: false)
        window.title = "Noon Studio · 沙特运营平台"
        window.minSize = NSSize(width: 900,height: 620)
        window.contentView = web
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        web.loadHTMLString("<html><meta charset='utf-8'><body style='font:18px -apple-system;padding:70px;background:#f4f6f7'><h1>Noon Studio</h1><p>正在打开本地运营资料…</p></body></html>", baseURL:nil)
        do {
            let support = FileManager.default.urls(for:.applicationSupportDirectory,in:.userDomainMask)[0]
            dataURL = ProcessInfo.processInfo.environment["NOON_STUDIO_DATA"].map { URL(fileURLWithPath:$0,isDirectory:true) } ?? support.appendingPathComponent("NoonStudio",isDirectory:true)
            try FileManager.default.createDirectory(at:dataURL,withIntermediateDirectories:true)
            ready = dataURL.appendingPathComponent("ready-\(UUID().uuidString).json")
            guard let resources = Bundle.main.resourceURL else { throw NSError(domain:"NoonStudio",code:1,userInfo:[NSLocalizedDescriptionKey:"应用资源缺失"] ) }
            let executable = resources.appendingPathComponent("backend/noon-backend")
            let process = Process()
            process.executableURL = executable
            process.arguments = ["--port","0","--data",dataURL.path,"--ready-file",ready.path]
            // Use only the bundled runtime; never inherit a Python installation from the shell.
            var env = ProcessInfo.processInfo.environment
            env.removeValue(forKey:"PYTHONPATH"); env.removeValue(forKey:"PYTHONHOME")
            process.environment = env
            let logURL=dataURL.appendingPathComponent("desktop.log")
            if !FileManager.default.fileExists(atPath:logURL.path) { FileManager.default.createFile(atPath:logURL.path,contents:nil) }
            logHandle=try FileHandle(forWritingTo:logURL); logHandle?.seekToEndOfFile()
            process.standardOutput=logHandle; process.standardError=logHandle
            process.terminationHandler = { [weak self] p in DispatchQueue.main.async { guard let self=self, !self.terminating else { return }; self.startupTimer?.invalidate(); self.showError("本地服务已停止（\(p.terminationStatus)）。资料仍保存在应用资料目录，请重新打开软件。") } }
            try process.run(); child=process
            startupTimer=Timer.scheduledTimer(withTimeInterval:0.2,repeats:true) { [weak self] _ in self?.checkReady() }
        } catch { showError(error.localizedDescription) }
    }
    func checkReady() {
        attempts += 1
        if let data=try? Data(contentsOf:ready), let object=(try? JSONSerialization.jsonObject(with:data)) as? [String:Any], let raw=object["url"] as? String, let url=URL(string:raw),url.host=="127.0.0.1",url.scheme=="http" {
            startupTimer?.invalidate(); baseURL=url; web.load(URLRequest(url:url))
        } else if attempts>150 { startupTimer?.invalidate(); showError("启动超时。请从文件菜单打开资料目录查看 desktop.log。") }
    }
    func showError(_ message:String) { let alert=NSAlert(); alert.messageText="Noon Studio 暂时无法运行"; alert.informativeText=message; alert.runModal() }
    func buildMenu() {
        let main=NSMenu(); let appItem=NSMenuItem(); main.addItem(appItem)
        let appMenu=NSMenu(); appItem.submenu=appMenu
        appMenu.addItem(withTitle:"关于 Noon Studio",action:#selector(about),keyEquivalent:"").target=self
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle:"退出 Noon Studio",action:#selector(NSApplication.terminate(_:)),keyEquivalent:"q")
        let file=NSMenuItem(); main.addItem(file); let fileMenu=NSMenu(title:"文件"); file.submenu=fileMenu
        fileMenu.addItem(withTitle:"打开资料目录",action:#selector(revealData),keyEquivalent:"d").target=self
        fileMenu.addItem(withTitle:"重新载入",action:#selector(reloadPage),keyEquivalent:"r").target=self
        let edit=NSMenuItem(); main.addItem(edit); let editMenu=NSMenu(title:"编辑"); edit.submenu=editMenu
        for (title,sel,key) in [("撤销","undo:","z"),("重做","redo:","Z"),("剪切","cut:","x"),("复制","copy:","c"),("粘贴","paste:","v"),("全选","selectAll:","a")] { editMenu.addItem(withTitle:title,action:Selector(sel),keyEquivalent:key) }
        NSApp.mainMenu=main
    }
    @objc func revealData(){if let data=dataURL { NSWorkspace.shared.open(data) }}
    @objc func reloadPage(){web.evaluateJavaScript("typeof dirty !== 'undefined' && dirty") { [weak self] value,_ in guard let self=self else{return}; if value as? Bool == true { let a=NSAlert(); a.messageText="有尚未保存的修改";a.informativeText="重新载入将放弃未保存的内容。";a.addButton(withTitle:"继续编辑");a.addButton(withTitle:"重新载入");if a.runModal() != .alertSecondButtonReturn{return} }; self.web.reload() }}
    @objc func about(){let a=NSAlert();a.messageText="Noon Studio " + (Bundle.main.object(forInfoDictionaryKey:"CFBundleShortVersionString") as? String ?? "");a.informativeText="面向 noon 沙特的本地运营平台。\n商品、媒体加工、模型服务、自动化流程与运营台账。\n真实店铺和供应商同步待接入。";a.runModal()}
    func applicationShouldTerminate(_ sender:NSApplication)->NSApplication.TerminateReply {
        if terminating { return .terminateNow }
        web.evaluateJavaScript("typeof dirty !== 'undefined' && dirty") { value,error in
            if let output=self.smokePath { try? "dirty=\(String(describing:value)); error=\(String(describing:error))".write(toFile:output+"-exit.txt",atomically:true,encoding:.utf8) }
            if value as? Bool == true {let a=NSAlert();a.messageText="有尚未保存的修改";a.informativeText="退出会放弃尚未保存的内容。";a.addButton(withTitle:"继续编辑");a.addButton(withTitle:"退出");if a.runModal() != .alertSecondButtonReturn { NSApp.reply(toApplicationShouldTerminate:false);return }}
            self.terminating=true; NSApp.reply(toApplicationShouldTerminate:true)
        }
        return .terminateLater
    }
    func applicationWillTerminate(_ notification:Notification){startupTimer?.invalidate();if child?.isRunning==true { child?.terminate() };if let ready=ready {try? FileManager.default.removeItem(at:ready)};try? logHandle?.close()}
    func applicationShouldTerminateAfterLastWindowClosed(_ sender:NSApplication)->Bool{return true}
    func applicationShouldHandleReopen(_ sender:NSApplication,hasVisibleWindows flag:Bool)->Bool{window.makeKeyAndOrderFront(nil);return true}
    func allowed(_ url:URL)->Bool { guard let base=baseURL else{return url.scheme=="about"};return url.scheme==base.scheme && url.host==base.host && url.port==base.port }
    func webView(_ webView:WKWebView,decidePolicyFor action:WKNavigationAction,decisionHandler:@escaping(WKNavigationActionPolicy)->Void){
        guard let url=action.request.url else {decisionHandler(.cancel);return}
        if action.shouldPerformDownload && (allowed(url)||url.scheme=="blob") {decisionHandler(.download);return}
        if allowed(url) || url.scheme=="about" {decisionHandler(.allow)} else {if ["https","http"].contains(url.scheme ?? "") && action.navigationType == .linkActivated {NSWorkspace.shared.open(url)};decisionHandler(.cancel)}
    }
    func webView(_ webView:WKWebView,createWebViewWith configuration:WKWebViewConfiguration,for action:WKNavigationAction,windowFeatures:WKWindowFeatures)->WKWebView? { if let url=action.request.url, ["https","http"].contains(url.scheme ?? "") {NSWorkspace.shared.open(url)};return nil }
    func webView(_ webView:WKWebView,runJavaScriptAlertPanelWithMessage message:String,initiatedByFrame frame:WKFrameInfo,completionHandler:@escaping()->Void){let a=NSAlert();a.messageText=message;a.beginSheetModal(for:window){_ in completionHandler()}}
    func webView(_ webView:WKWebView,runJavaScriptConfirmPanelWithMessage message:String,initiatedByFrame frame:WKFrameInfo,completionHandler:@escaping(Bool)->Void){let a=NSAlert();a.messageText=message;a.addButton(withTitle:"确认");a.addButton(withTitle:"取消");a.beginSheetModal(for:window){completionHandler($0 == .alertFirstButtonReturn)}}
    func webView(_ webView:WKWebView,runOpenPanelWith parameters:WKOpenPanelParameters,initiatedByFrame frame:WKFrameInfo,completionHandler:@escaping([URL]?) -> Void) {
        let panel=NSOpenPanel()
        panel.allowsMultipleSelection=parameters.allowsMultipleSelection
        panel.canChooseDirectories=parameters.allowsDirectories
        panel.canChooseFiles = !parameters.allowsDirectories
        panel.beginSheetModal(for:window) { response in
            completionHandler(response == .OK ? panel.urls:nil)
        }
    }
    func webView(_ webView:WKWebView,navigationAction:WKNavigationAction,didBecome download:WKDownload){download.delegate=self}
    func download(_ download:WKDownload,decideDestinationUsing response:URLResponse,suggestedFilename:String,completionHandler:@escaping(URL?)->Void){let p=NSSavePanel();p.nameFieldStringValue=suggestedFilename;p.beginSheetModal(for:window){result in let url=result == .OK ? p.url:nil;if let url=url {self.downloadTargets[ObjectIdentifier(download)]=url};completionHandler(url)}}
    func downloadDidFinish(_ download:WKDownload){if let url=downloadTargets.removeValue(forKey:ObjectIdentifier(download)){NSWorkspace.shared.activateFileViewerSelecting([url])}}
    func download(_ download:WKDownload,didFailWithError error:Error,resumeData:Data?){downloadTargets.removeValue(forKey:ObjectIdentifier(download));showError("导出文件未完成："+error.localizedDescription)}
    func webView(_ webView:WKWebView,didFinish navigation:WKNavigation!) {
        guard let output=smokePath,baseURL != nil,!smokeDone else{return}
        smokeDone=true
        DispatchQueue.main.asyncAfter(deadline:.now()+2) {
            webView.evaluateJavaScript("JSON.stringify({title:document.title,heading:document.querySelector('h1')?.textContent,nav:[...document.querySelectorAll('[data-nav]')].map(x=>x.textContent),overflow:document.documentElement.scrollWidth>innerWidth,backend:typeof state!=='undefined'&&!!state.ops,products:typeof state!=='undefined'?state.products.length:null})") {value,error in
                let result:[String:Any]=["ui":value ?? "", "error":error?.localizedDescription ?? "", "url":self.baseURL!.absoluteString, "pid":self.child?.processIdentifier ?? 0]
                if let data=try? JSONSerialization.data(withJSONObject:result,options:.prettyPrinted){try? data.write(to:URL(fileURLWithPath:output+".json"))}
                webView.takeSnapshot(with:nil){image,error in if let image=image, let tiff=image.tiffRepresentation,let bitmap=NSBitmapImageRep(data:tiff),let png=bitmap.representation(using:.png,properties:[:]){try? png.write(to:URL(fileURLWithPath:output+".png"))} }
            }
        }
    }
}
let app=NSApplication.shared
let delegate=Studio()
app.delegate=delegate
app.run()
