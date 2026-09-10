import QtQuick
import Quickshell

ShellRoot {
    BoundedProcess {
        id: process
        timeoutMs: 200
        onFinished: function (code) {
            var result = code === 0 && output === "hello" ? "success" : failure;
            console.log("RESULT:" + result);
            Qt.quit();
        }
    }
    Component.onCompleted: process.start([Quickshell.env("QS_TEST_HELPER")])
}
