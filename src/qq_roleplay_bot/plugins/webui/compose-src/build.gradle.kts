import org.jetbrains.kotlin.gradle.ExperimentalWasmDsl

plugins {
    alias(libs.plugins.kotlinMultiplatform)
    alias(libs.plugins.composeMultiplatform)
    alias(libs.plugins.composeCompiler)
}

kotlin {
    @OptIn(ExperimentalWasmDsl::class)
    wasmJs {
        outputModuleName = "composeApp"
        browser {
            commonWebpackConfig {
                outputFileName = "composeApp.js"
            }
        }
        binaries.executable()
    }

    sourceSets {
        val wasmJsMain by getting {
            dependencies {
                implementation(compose.runtime)
                implementation(compose.foundation)
                implementation(compose.ui)
                // Miuix itself, published wasm-js klib.
                implementation(libs.miuix.ui)
                implementation(libs.miuix.core)
                // Icon set. `miuix-ui` only ships seven basic icons (ArrowRight / ArrowUpDown /
                // Check / Close / Search / SearchCleanup / Sidebar) — not enough to name six groups
                // and 23 leaves. `miuix-icons` is pure `ImageVector` data (it pulls neither
                // `miuix-blur` nor `miuix-shader`), so it adds bytes, not runtime risk; the wasm
                // compile + zero-console-error result is recorded in NOTES.md §依赖.
                implementation(libs.miuix.icons)
            }
        }
    }
}
