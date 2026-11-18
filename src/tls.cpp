#include "spe/tls.hpp"

#include <curl/curl.h>
#ifdef __APPLE__
#include <CommonCrypto/CommonDigest.h>
#else
#include <openssl/evp.h>
#endif

#include <array>
#include <cerrno>
#include <cstdint>
#include <fcntl.h>
#include <stdexcept>
#include <sys/stat.h>
#include <system_error>
#include <unistd.h>

namespace spe {
CaBundle load_ca_bundle(const std::string& path) {
    constexpr std::size_t limit = 1024 * 1024;
    const auto fd = ::open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NONBLOCK);
    if (fd < 0) throw std::system_error(errno, std::generic_category(), "cannot open CA certificate file");
    struct Descriptor { int value; ~Descriptor() { ::close(value); } } owner{fd};
    struct stat info{};
    if (::fstat(fd, &info) != 0) throw std::system_error(errno, std::generic_category(), "cannot inspect CA certificate file");
    if (!S_ISREG(info.st_mode) || info.st_size <= 0 || static_cast<std::uintmax_t>(info.st_size) > limit)
        throw std::invalid_argument("CA certificate file must be a nonempty regular file of at most 1 MiB");
    CaBundle result;
    std::array<char, 4096> buffer{};
    while (true) {
        const auto n = ::read(fd, buffer.data(), buffer.size());
        if (n < 0 && errno == EINTR) continue;
        if (n < 0) throw std::system_error(errno, std::generic_category(), "cannot read CA certificate file");
        if (n == 0) break;
        result.bytes.append(buffer.data(), static_cast<std::size_t>(n));
        if (result.bytes.size() > limit) throw std::invalid_argument("CA certificate file exceeds 1 MiB");
    }
    if (result.bytes.empty()) throw std::invalid_argument("CA certificate file is empty");
    std::array<unsigned char, 32> digest{};
#ifdef __APPLE__
    if (CC_SHA256(result.bytes.data(), static_cast<CC_LONG>(result.bytes.size()), digest.data()) == nullptr)
        throw std::runtime_error("cannot fingerprint CA certificate bundle");
#else
    unsigned length = 0;
    if (EVP_Digest(result.bytes.data(), result.bytes.size(), digest.data(), &length, EVP_sha256(), nullptr) != 1 || length != digest.size())
        throw std::runtime_error("cannot fingerprint CA certificate bundle");
#endif
    constexpr char hex[] = "0123456789abcdef";
    for (auto byte : digest) { result.sha256 += hex[byte >> 4]; result.sha256 += hex[byte & 15]; }
    return result;
}

bool is_tls_error(int code) {
    switch (static_cast<CURLcode>(code)) {
    case CURLE_SSL_CONNECT_ERROR:
    case CURLE_PEER_FAILED_VERIFICATION:
    case CURLE_SSL_CERTPROBLEM:
    case CURLE_SSL_CIPHER:
    case CURLE_SSL_CACERT_BADFILE:
    case CURLE_SSL_CRL_BADFILE:
    case CURLE_SSL_ISSUER_ERROR:
    case CURLE_SSL_PINNEDPUBKEYNOTMATCH:
    case CURLE_SSL_INVALIDCERTSTATUS:
    case CURLE_SSL_CLIENTCERT:
    case CURLE_SSL_ENGINE_NOTFOUND:
    case CURLE_SSL_ENGINE_SETFAILED:
    case CURLE_SSL_ENGINE_INITFAILED:
    case CURLE_SSL_SHUTDOWN_FAILED:
        return true;
    default:
        return false;
    }
}
}
