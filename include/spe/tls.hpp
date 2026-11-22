#pragma once

#include <string>
#include <string_view>

namespace spe {
// Stable bytes and identity of an explicitly selected, bounded CA bundle.
// Contents stay private to the run; only the digest belongs in result metadata.
struct CaBundle {
    std::string bytes;
    std::string sha256;
};

std::string sha256_hex(std::string_view bytes);
CaBundle load_ca_bundle(const std::string& path);
bool is_tls_error(int curl_code);
}
