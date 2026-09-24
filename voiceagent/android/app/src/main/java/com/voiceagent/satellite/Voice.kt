package com.voiceagent.satellite

import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/** Speaks one sentence and returns when it has finished playing. */
fun interface Tts {
    fun speak(text: String, lang: String?)
}

/**
 * Speaks queued sentences in order and keeps the mic muted while it does, plus a
 * short tail so a Bluetooth speaker's delayed audio isn't streamed back.
 * Same behaviour as Voice in satellite.py.
 */
class Voice(
    private val tts: Tts,
    private val tailMs: Long,
    private val log: Logger = Logger.NONE,
) {
    private sealed interface Job {
        class Speak(val text: String, val lang: String?) : Job
        class Pause(val ms: Long) : Job
        class End(val after: (() -> Unit)?) : Job
    }

    private val queue = LinkedBlockingQueue<Job>()
    private val mutedFlag = AtomicBoolean(false)
    private val worker = thread(isDaemon = true, name = "voice") { work() }

    val muted: Boolean get() = mutedFlag.get()

    fun speak(text: String, lang: String?) {
        mutedFlag.set(true)
        queue.put(Job.Speak(text, lang))
    }

    /** Marks the end of a reply. [after] runs once everything before it was spoken. */
    fun end(after: (() -> Unit)? = null) {
        queue.put(Job.End(after))
    }

    fun pause(ms: Long) {
        mutedFlag.set(true)
        queue.put(Job.Pause(ms))
    }

    fun shutdown() {
        worker.interrupt()
    }

    private fun work() {
        while (true) {
            val job = try {
                queue.take()
            } catch (e: InterruptedException) {
                return
            }
            try {
                when (job) {
                    is Job.Speak -> tts.speak(job.text, job.lang)
                    is Job.Pause -> {
                        Thread.sleep(job.ms)
                        if (queue.isEmpty()) mutedFlag.set(false)
                    }
                    is Job.End -> {
                        Thread.sleep(tailMs)
                        if (queue.isEmpty()) mutedFlag.set(false)
                        job.after?.invoke()
                    }
                }
            } catch (e: InterruptedException) {
                return
            } catch (e: Exception) {
                log.log("voice worker", e)
            }
        }
    }
}
