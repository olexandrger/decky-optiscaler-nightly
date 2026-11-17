// Stand-in for a game, used by scripts/test_live_asi.py under Wine.
//
// Loads OptiScaler the way a game does (as the proxy dll it is installed as,
// dxgi.dll by default), which makes OptiScaler read its ini and load the ASI
// plugins in plugins/, then keeps the process alive so the live-control ASI can
// be driven through its command files. Exits on its own after the given number
// of seconds, or when "host.quit" appears next to it.
#include <windows.h>

#include <stdio.h>
#include <stdlib.h>

int wmain(int argc, wchar_t** argv) {
    const wchar_t* proxy = argc > 1 ? argv[1] : L"dxgi.dll";
    int seconds = argc > 2 ? _wtoi(argv[2]) : 120;
    HMODULE module = LoadLibraryW(proxy);
    printf("LoadLibrary(%ls) = %p (error %lu)\n", proxy, (void*) module,
           module ? 0ul : GetLastError());
    fflush(stdout);
    if (!module) return 2;
    for (int tick = 0; tick < seconds * 10; tick++) {
        if (GetFileAttributesW(L"host.quit") != INVALID_FILE_ATTRIBUTES) break;
        Sleep(100);
    }
    return 0;
}
