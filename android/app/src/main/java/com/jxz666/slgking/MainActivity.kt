package com.jxz666.slgking

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import com.jxz666.slgking.ui.AppRoot
import com.jxz666.slgking.ui.SlgkingTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)
        com.jxz666.slgking.ui.ThemePrefs.load(this)
        setContent {
            SlgkingTheme {
                AppRoot()
            }
        }
    }
}
