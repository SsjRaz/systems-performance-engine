#pragma once

#include <string>

namespace spe {
// Stable bytes and identity of an explicitly selected, bounded CA bundle.
// Contents stay private to the run; only the digest belongs in result metadata.
struct CaBundle {
    std::string bytes;
    std::string sha256;
};

CaBundle load_ca_bundle(const std::string& path);
bool is_tls_error(int curl_code);
}
