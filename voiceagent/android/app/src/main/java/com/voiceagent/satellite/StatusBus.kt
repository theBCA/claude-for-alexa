package com.voiceagent.satellite

import java.util.concurrent.CopyOnWriteArraySet

/** Carries the service's status line to the screen, if it is open. */
object StatusBus {
    @Volatile
    var last: String = "Stopped"
        private set

    private val listeners = CopyOnWriteArraySet<(String) -> Unit>()

    fun post(status: String) {
        last = status
        listeners.forEach { it(status) }
    }

    /** Returns a function that removes the listener. */
    fun listen(listener: (String) -> Unit): () -> Unit {
        listeners.add(listener)
        return { listeners.remove(listener) }
    }
}
