package com.rgbmo.facecap

import java.net.HttpURLConnection
import java.net.URL
import java.security.SecureRandom
import java.security.cert.X509Certificate
import javax.net.ssl.HostnameVerifier
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManager
import javax.net.ssl.X509TrustManager

object Net {
    private val trust = object : X509TrustManager {
        override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) {}
        override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {}
        override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
    }

    fun get(url: String): String? = exchange(url, "GET", null)
    fun post(url: String, body: String): Boolean = exchange(url, "POST", body) != null

    private fun exchange(url: String, method: String, body: String?): String? {
        return try {
            val connection = URL(url).openConnection() as HttpURLConnection
            if (connection is HttpsURLConnection) {
                val context = SSLContext.getInstance("TLS")
                context.init(null, arrayOf<TrustManager>(trust), SecureRandom())
                connection.sslSocketFactory = context.socketFactory
                connection.hostnameVerifier = HostnameVerifier { _, _ -> true }
            }
            connection.requestMethod = method
            connection.connectTimeout = 2000
            connection.readTimeout = 2000
            if (body != null) {
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json")
                connection.outputStream.use { it.write(body.toByteArray()) }
            }
            val code = connection.responseCode
            val text = (if (code in 200..299) connection.inputStream else connection.errorStream)
                ?.bufferedReader()?.use { it.readText() }
            if (code in 200..299) text else null
        } catch (_: Exception) {
            null
        }
    }
}
