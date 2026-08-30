#define VERSION "1.3"
#define REPOSITORY "https://github.com/offici5l/MiAssistantTool"

#ifdef _WIN32
#include <libusb.h>
#else
#include <libusb-1.0/libusb.h>
#endif

#include <string.h>
#include <unistd.h>
#include <openssl/evp.h>
#include <curl/curl.h>

#include "tiny-json/tiny-json.h"

#define ADB_CLASS 0xff
#define ADB_SUB_CLASS 0x42
#define ADB_PROTOCOL_CODE 1
#define ADB_CONNECT 0x4E584E43
#define ADB_VERSION 0x01000001
#define ADB_OPEN 0x4E45504F
#define ADB_OKAY 0x59414B4F
#define ADB_WRTE 0x45545257
#define ADB_CLSE 0x45534C43
#define ADB_TRANSFER_DONE 0x00000000
#define ADB_MAX_DATA 1024 * 1024
#define ADB_SIDELOAD_CHUNK_SIZE 1024 * 64

char device[80];
char version[80];
char sn[80];
char codebase[80];
char branch[80];
char language[80];
char region[80];
char romzone[80];

int bulk_in;
int bulk_out;
int interface_num;

libusb_context *ctx;
libusb_device_handle *dev_handle;

char response[4096]; 

typedef struct {
    uint32_t cmd;
    uint32_t arg0;
    uint32_t arg1;
    uint32_t len;
    uint32_t checksum;
    uint32_t magic;
} adb_usb_packet;

int usb_read(void *data, int datalen) {
    int read_len;
    libusb_set_option(NULL, LIBUSB_OPTION_NO_DEVICE_DISCOVERY);
    int r = libusb_bulk_transfer(dev_handle, bulk_in, data, datalen, &read_len, 1000);
    if (r != LIBUSB_SUCCESS) {
        return -1;
    }
    return read_len;
}

int usb_write(void *data, int datalen) {
    int write_len;
    libusb_set_option(NULL, LIBUSB_OPTION_NO_DEVICE_DISCOVERY);
    int r = libusb_bulk_transfer(dev_handle, bulk_out, data, datalen, &write_len, 1000);
    if (r != LIBUSB_SUCCESS) {
        return -1;
    }
    return write_len;
}

int send_command(uint32_t cmd, uint32_t arg0, uint32_t arg1, void *data, int datalen) {
    adb_usb_packet pkt;
    pkt.cmd = cmd;
    pkt.arg0 = arg0;
    pkt.arg1 = arg1;
    pkt.len = datalen;
    pkt.checksum = 0;
    pkt.magic = cmd ^ 0xffffffff;

    if(usb_write(&pkt, sizeof(pkt)) == -1) {
        return 1;
    } 

    if(datalen > 0) {
        if(usb_write(data, datalen) == -1) {
            return 1;
        }
    }
    return 0;
}

int recv_packet(adb_usb_packet *pkt, void* data, int *data_len) {
    if(!usb_read(pkt, sizeof(adb_usb_packet))) {
        return 1;
    }

    if(pkt->len > 0) {
        if(!usb_read(data, pkt->len)) {
            return 1;
        }
    }

    *data_len = pkt->len;
    return 0;
}



char* adb_cmd(char* command) {
    int cmd_len = strlen(command);
    char cmd[cmd_len + 1];
    memcpy(cmd, command, cmd_len);
    cmd[cmd_len] = 0;

    if (send_command(ADB_OPEN, 1, 0, cmd, cmd_len)) {
        printf("device not accept connect request\n");
        return NULL;
    }

    adb_usb_packet pkt;
    char data[512];
    int data_len;
    recv_packet(&pkt, data, &data_len);

    if (recv_packet(&pkt, response, &data_len)) {
        printf("Failed to get info from device\n");
        return NULL;
    }

    response[data_len] = 0;

    if (response[data_len - 1] == '\n')
        response[data_len - 1] = 0;

    recv_packet(&pkt, data, &data_len);

    return response;
}

void calculate_md5(char *filePath, char *md5) {
    FILE *file;

    while (1) {
        printf("Enter .zip file path: ");
        if (fgets(filePath, 256, stdin)) {
            filePath[strcspn(filePath, "\n")] = '\0';
            if (strstr(filePath, ".zip") && (file = fopen(filePath, "rb"))) {
                fclose(file);
                break;
            }
        }
        printf("Invalid file, try again.\n");
    }

    file = fopen(filePath, "rb");
    EVP_MD_CTX *mdctx = EVP_MD_CTX_new();
    EVP_DigestInit_ex(mdctx, EVP_md5(), NULL);
    unsigned char data[1024], md5hash[EVP_MAX_MD_SIZE];
    size_t bytesRead;
    unsigned int md5len;

    while ((bytesRead = fread(data, 1, sizeof(data), file)) > 0)
        EVP_DigestUpdate(mdctx, data, bytesRead);

    EVP_DigestFinal_ex(mdctx, md5hash, &md5len);
    fclose(file);
    EVP_MD_CTX_free(mdctx);

    for (unsigned int i = 0; i < md5len; i++)
        sprintf(&md5[i * 2], "%02x", md5hash[i]);

    md5[md5len * 2] = '\0';
}

const char *validate_check(const char *md5, int flash) {
    const unsigned char key[16] = { 0x6D, 0x69, 0x75, 0x69, 0x6F, 0x74, 0x61, 0x76, 0x61, 0x6C, 0x69, 0x64, 0x65, 0x64, 0x31, 0x31 };
    const unsigned char iv[16] = { 0x30, 0x31, 0x30, 0x32, 0x30, 0x33, 0x30, 0x34, 0x30, 0x35, 0x30, 0x36, 0x30, 0x37, 0x30, 0x38 };

    char json_request[1024];
    sprintf(json_request, "{\"d\":\"%s\",\"v\":\"%s\",\"c\":\"%s\",\"b\":\"%s\",\"sn\":\"%s\",\"l\":\"en-US\",\"f\":\"1\",\"options\":{\"zone\":%s},\"pkg\":\"%s\"}", device, version, codebase, branch, sn, romzone, md5);

    int len = strlen(json_request);
    int mod_len = 16 - (len % 16);
    if (mod_len > 0) memset(json_request + len, mod_len, mod_len), len += mod_len;

    unsigned char enc_out[1024]; 
    int enc_out_len = 0;

    EVP_CIPHER_CTX *ctx = EVP_CIPHER_CTX_new();
    if (!ctx) return NULL;

    if (EVP_EncryptInit_ex(ctx, EVP_aes_128_cbc(), NULL, key, iv) != 1) {
        EVP_CIPHER_CTX_free(ctx);
        return NULL;
    }

    int update_len = 0;
    if (EVP_EncryptUpdate(ctx, enc_out, &update_len, (unsigned char*)json_request, len) != 1) {
        EVP_CIPHER_CTX_free(ctx);
        return NULL;
    }
    enc_out_len += update_len;

    int final_len = 0;
    if (EVP_EncryptFinal_ex(ctx, enc_out + enc_out_len, &final_len) != 1) {
        EVP_CIPHER_CTX_free(ctx);
        return NULL;
    }
    enc_out_len += final_len;

    EVP_CIPHER_CTX_free(ctx);

    char encoded_buf[EVP_ENCODE_LENGTH(enc_out_len)];
    EVP_EncodeBlock((unsigned char*)encoded_buf, enc_out, enc_out_len);

    CURL *curl = curl_easy_init();
    if (!curl) return NULL;

    char *json_post_data = curl_easy_escape(curl, encoded_buf, strlen(encoded_buf));
    if (!json_post_data) { 
        curl_easy_cleanup(curl); 
        return NULL; 
    }

    size_t post_buf_len = strlen(json_post_data) + strlen("q=&t=&s=1") + 1;
    /* post_buf is reused below to hold the server's decoded/decrypted response
     * (EVP_DecodeBlock + AES-CBC decrypt write back into it), which is
     * typically larger than the outgoing request. Sizing it to only the
     * request length caused a heap buffer overflow (observed as
     * STATUS_HEAP_CORRUPTION on Windows) as soon as the response didn't fit. */
    if (post_buf_len < 8192) post_buf_len = 8192;
    unsigned char *post_buf = (unsigned char *)malloc(post_buf_len);
    if (!post_buf) {
        curl_free(json_post_data);
        curl_easy_cleanup(curl);
        return NULL;
    }

    snprintf((char*)post_buf, post_buf_len, "q=%s&t=&s=1", json_post_data);
    curl_free(json_post_data);

    FILE *response_file = fopen("response.tmp", "wb");
    if (!response_file) {
        perror("Error opening file for writing");
        free(post_buf);
        curl_easy_cleanup(curl);
        return NULL;
    }

    curl_easy_setopt(curl, CURLOPT_URL, "http://update.miui.com/updates/miotaV3.php");
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "MiTunes_UserAgent_v3.0");
    curl_easy_setopt(curl, CURLOPT_POST, 1);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, post_buf);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, fwrite);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, response_file);

    if (curl_easy_perform(curl) != CURLE_OK) {
        perror("Error during curl_easy_perform");
        fclose(response_file);
        free(post_buf);
        curl_easy_cleanup(curl);
        return NULL;
    }
    fclose(response_file);
    /* post_buf is NOT freed here (unlike the error path above): it is
     * reused below to hold the decoded/decrypted response and is read
     * through json_create()'s in-place parsing for the rest of this
     * function. Freeing it here was a use-after-free on every successful
     * request - the buffer would still be reused right after being freed. */
    curl_easy_cleanup(curl);

    FILE *response_file_read = fopen("response.tmp", "rb");
    if (!response_file_read) {
        perror("Failed to open file for reading");
        return NULL;
    }

    fseek(response_file_read, 0, SEEK_END);
    long response_size = ftell(response_file_read);
    fseek(response_file_read, 0, SEEK_SET);

    char *response_buffer = malloc(response_size + 1);
    if (!response_buffer) {
        perror("Memory allocation failed");
        fclose(response_file_read);
        return NULL;
    }

    fread(response_buffer, 1, response_size, response_file_read);
    response_buffer[response_size] = '\0';
    fclose(response_file_read);

    int decoded_len = EVP_DecodeBlock((unsigned char*)post_buf, (unsigned char*)response_buffer, response_size);
    free(response_buffer);
    remove("response.tmp");

    ctx = EVP_CIPHER_CTX_new();
    if (!ctx) return NULL;

    EVP_DecryptInit_ex(ctx, EVP_aes_128_cbc(), NULL, key, iv);
    int plain_len = 0, temp_len = 0;
    EVP_DecryptUpdate(ctx, post_buf, &plain_len, post_buf, decoded_len);
    EVP_DecryptFinal_ex(ctx, post_buf + plain_len, &temp_len);
    EVP_CIPHER_CTX_free(ctx);

    char *start = strchr((char*)post_buf, '{');
    char *end = strrchr((char*)post_buf, '}');
    if (!start || !end) return NULL;

    memmove(post_buf, start, end - start + 1);
    post_buf[end - start + 1] = '\0';
    // printf("Response after decryption: %s\n", post_buf); 
    json_t pool[10000];
    json_t const *parsed_json = json_create((char *)post_buf, pool, 10000);
    if (!parsed_json) return NULL;

    if (flash == 1) {
        json_t const *pkg_rom = json_getProperty(parsed_json, "PkgRom");
        if (pkg_rom) {
            int Erase = atoi(json_getValue(json_getProperty(pkg_rom, "Erase")));
            if (Erase == 1) {
                printf("NOTICE: Data will be erased during flashing.\nPress Enter to continue...");
                getchar(); 
           }
            json_t const *validate = json_getProperty(pkg_rom, "Validate");
            return json_getValue(validate);
        } else {
            json_t const *code = json_getProperty(parsed_json, "Code");
            json_t const *message = json_getProperty(code, "message");
            printf("\n%s\n", json_getValue(message));
            return NULL;
        }
    } else {
        if (json_getType(parsed_json) == JSON_OBJ) {
            json_t const *child = json_getChild(parsed_json);
            /* json_getSibling()/json_getName() dereference their argument with
             * no NULL check (tiny-json), so a response object with 0 or 1
             * property crashed here with a NULL pointer dereference. */
            json_t const *firstSibling = child ? json_getSibling(child) : NULL;
            if (firstSibling && (strcmp(json_getName(firstSibling), "Signup") == 0 || strcmp(json_getName(firstSibling), "VersionBoot") == 0)) {
                fprintf(stderr, "Error: Invalid data\n");
                return NULL;
            }
            while (child) {
                child = json_getSibling(child);
                if (!child || strcmp(json_getName(child), "Icon") == 0) {
                    break;
                }
                json_t const *cA = json_getProperty(parsed_json, json_getName(child));
                if (cA) {
                    json_t const *md5 = json_getProperty(cA, "md5");
                    if (md5) {
                        printf("\n%s: %s\nmd5: %s\n", json_getName(child), json_getValue(json_getProperty(cA, "name")), json_getValue(md5));
                    } 
                }     
            }
        }
        return NULL;
    }
}

int start_sideload(const char *sideload_file, const char *validate) {

    printf("\n\n");
    /* "rb": must be binary, not text mode. On Windows the CRT's text-mode
     * translation treats byte 0x1A as EOF and rewrites CRLF sequences,
     * silently truncating/corrupting the ROM data being streamed below. */
    FILE *fp = fopen(sideload_file, "rb");
    if (!fp) {
        perror("Failed to open file");
        return 1;
    }

    /* file_size/offsets must be 64-bit: `long` is 32-bit on Windows (LLP64)
     * even in a 64-bit build, so it silently overflows/wraps for any ROM
     * over ~2GB (i.e. basically every recovery ROM this tool flashes). That
     * corrupts the sideload-host size announced to the device and the
     * per-chunk file offsets, aborting the transfer partway through. */
#ifdef _WIN32
    _fseeki64(fp, 0, SEEK_END);
    long long file_size = _ftelli64(fp);
    _fseeki64(fp, 0, SEEK_SET);
#else
    fseeko(fp, 0, SEEK_END);
    long long file_size = ftello(fp);
    fseeko(fp, 0, SEEK_SET);
#endif
    char sideload_host_command[128 + strlen(validate)];
    memset(sideload_host_command, 0, sizeof(sideload_host_command));
    sprintf(sideload_host_command, "sideload-host:%lld:%d:%s:0", file_size, ADB_SIDELOAD_CHUNK_SIZE, validate);

    send_command(ADB_OPEN, 1, 0, sideload_host_command, strlen(sideload_host_command) + 1);

    uint8_t *work_buffer = malloc(ADB_SIDELOAD_CHUNK_SIZE);
    if (!work_buffer) {
        perror("Failed to allocate memory");
        fclose(fp);
        return 1;
    }

    char dummy_data[64];
    int dummy_data_size;
    adb_usb_packet pkt;
    long long total_sent = 0;

    while (1) {
        pkt.cmd = 0;
        recv_packet(&pkt, dummy_data, &dummy_data_size);

        dummy_data[dummy_data_size] = 0;
        if(dummy_data_size > 8) {
            printf("\n\n%s\n\n", dummy_data);
            break;
        }

        if (pkt.cmd == ADB_OKAY) {
            send_command(ADB_OKAY, pkt.arg1, pkt.arg0, NULL, 0);
        }

        if (pkt.cmd == ADB_TRANSFER_DONE && total_sent > 0) {
            //struct timespec ts = {6, 0};
            //nanosleep(&ts, NULL);
            //continue;
        }

        if (pkt.cmd != ADB_WRTE) {
            continue;
        }

        long long block = strtoll(dummy_data, NULL, 10);
        long long offset = block * ADB_SIDELOAD_CHUNK_SIZE;
        if (offset > file_size) break;
        int to_write = ADB_SIDELOAD_CHUNK_SIZE;
        if(offset + ADB_SIDELOAD_CHUNK_SIZE > file_size)
            to_write = file_size - offset;
#ifdef _WIN32
        _fseeki64(fp, offset, SEEK_SET);
#else
        fseeko(fp, offset, SEEK_SET);
#endif
        fread(work_buffer, 1, to_write, fp);
        send_command(ADB_WRTE, pkt.arg1, pkt.arg0, work_buffer, to_write);
        send_command(ADB_OKAY, pkt.arg1, pkt.arg0, NULL, 0);
        total_sent += to_write;

        printf("\rFlashing in progress ... %d/100%%", (int)(((float)total_sent / (1024 * 1024 * 1024) / 2) * 100 / (file_size / (1024 * 1024 * 1024))) > 100 ? 100 : (int)(((float)total_sent / (1024 * 1024 * 1024) / 2) * 100 / (file_size / (1024 * 1024 * 1024))));
        fflush(stdout);

    }

    free(work_buffer);
    fclose(fp);
    return 0;
}


int check_device(libusb_device *dev) {

    struct libusb_device_descriptor desc;
    int r = libusb_get_device_descriptor(dev, &desc);
    struct libusb_config_descriptor *configs;
    r = libusb_get_active_config_descriptor(dev, &configs);

    bulk_in = -1;
    bulk_out = -1;
    interface_num = -1;
    for (int i = 0; i < configs->bNumInterfaces; i++) {
        struct libusb_interface intf = configs->interface[i];
        if (intf.num_altsetting == 0) {
            continue;
        }
        interface_num = i;
        struct libusb_interface_descriptor intf_desc = intf.altsetting[0];

        if (!(intf_desc.bInterfaceClass == ADB_CLASS && intf_desc.bInterfaceSubClass == ADB_SUB_CLASS && intf_desc.bInterfaceProtocol == ADB_PROTOCOL_CODE)) {
            continue;
        }
        if (intf.num_altsetting != 1) {
            continue;
        }

        for(int endpoint_num = 0; endpoint_num < intf_desc.bNumEndpoints; endpoint_num++) {
            struct libusb_endpoint_descriptor ep = intf_desc.endpoint[endpoint_num];
            const uint8_t endpoint_addr = ep.bEndpointAddress;
            const uint8_t endpoint_attr = ep.bmAttributes;
            const uint8_t transfer_type = endpoint_attr & LIBUSB_TRANSFER_TYPE_MASK;
            if (transfer_type != LIBUSB_TRANSFER_TYPE_BULK) {
                continue;
            }
            if ((endpoint_addr & LIBUSB_ENDPOINT_DIR_MASK) == LIBUSB_ENDPOINT_OUT && bulk_out == -1) {
                bulk_out = endpoint_addr;
            } else if ((endpoint_addr & LIBUSB_ENDPOINT_DIR_MASK) != LIBUSB_ENDPOINT_OUT && bulk_in == -1) {
                bulk_in = endpoint_addr;
            }

            if(bulk_out != -1 && bulk_in != -1) {
                return 0;
            }
        }
    }
    return 1;
}

int main(int argc, char *argv[]) {

    if (argc == 1) {
        printf("\nVERSION: %s\n", VERSION);
        printf("Repository: %s\n\n", REPOSITORY);
        const char *choices[] = {"Read Info", "ROMs that can be flashed", "Flash Official Recovery ROM", "Format Data", "Reboot"};
        printf("\nUsage: %s \033[0;32m<choice>\033[0m\n\n  \033[0;32mchoice\033[0m > description\n\n", argv[0]);
        for (int i = 0; i < 5; i++)
            printf("  \033[0;32m%d\033[0m > %s\n\n", i + 1, choices[i]);
        return 0;
    }

    int choice = atoi(argv[1]);
    if (choice < 1 || choice > 5) {
        printf("Invalid choice\n");
        return 1;
    }


    #ifdef _WIN32
        int method = 2;
    #else
        int method = (getenv("PREFIX") && access("/data/data/com.termux", F_OK) != -1) ? (geteuid() == 0 ? 2 : 1) : 2;
    #endif

    libusb_init(&ctx);

    if (method == 1) {
        const char *fd = getenv("TERMUX_USB_FD");
        if (fd == NULL) {
            printf("\n\nWithout root (termux-usb must be used)\n\n");
            return 1;
        }
        libusb_set_option(NULL, LIBUSB_OPTION_NO_DEVICE_DISCOVERY);
        libusb_wrap_sys_device(ctx, (intptr_t)atoi(fd), &dev_handle);
        if (check_device(libusb_get_device(dev_handle))) {
            printf("\n\ndevice is not connected, or not in mi assistant mode\n\n");
            return 1;
        }
    } else {
        libusb_device **devs = NULL;
        ssize_t cnt = libusb_get_device_list(ctx, &devs);
        int i = 0;
        libusb_device *dev = NULL;
        while ((dev = devs[i++]) != NULL && check_device(dev) != 0);
        if (!dev) {
            printf("\n\ndevice is not connected, or not in mi assistant mode\n\n");
            return 1;
        } else {
            libusb_open(dev, &dev_handle) || libusb_claim_interface(dev_handle, interface_num);
        }        
    }

    int result = send_command(ADB_CONNECT, ADB_VERSION, ADB_MAX_DATA, "host::\x0", 7);
    char buf[512];
    adb_usb_packet pkt;
    if (result || recv_packet(&pkt, buf, &(int){sizeof(buf)}) || memcmp(buf, "sideload::", 10)) {
        printf("\nFailed to connect with device\n");
        return 1;
    }

    strncpy(device, adb_cmd("getdevice:"), sizeof(device) - 1);
    strncpy(version, adb_cmd("getversion:"), sizeof(version) - 1);
    strncpy(sn, adb_cmd("getsn:"), sizeof(sn) - 1);
    strncpy(codebase, adb_cmd("getcodebase:"), sizeof(codebase) - 1);
    strncpy(branch, adb_cmd("getbranch:"), sizeof(branch) - 1);
    strncpy(language, adb_cmd("getlanguage:"), sizeof(language) - 1);
    strncpy(region, adb_cmd("getregion:"), sizeof(region) - 1);
    strncpy(romzone, adb_cmd("getromzone:"), sizeof(romzone) - 1);

    switch (choice) {
        case 1: {
            printf("\n\nDevice: %s\n", device);
            printf("Version: %s\n", version);
            printf("Serial Number: %s\n", sn);
            printf("Codebase: %s\n", codebase);
            printf("Branch: %s\n", branch);
            printf("Language: %s\n", language);
            printf("Region: %s\n", region);
            printf("ROM Zone: %s\n\n", romzone);
            break;
        }
        case 2: {
            validate_check("", 0);
            break;
        }
        case 3: {
            char filePath[256], md5[65];
            calculate_md5(filePath, md5);
            const char *validate = validate_check(md5, 1);
            if (validate) {
                start_sideload(filePath, validate);
            } 
            break;
        }
        case 4: {
            char *format = adb_cmd("format-data:");
            printf("\n%s\n", format);
            char *reboot = adb_cmd("reboot:");
            printf("\n%s\n", reboot);
            break;
        }
        case 5: {
            char *reboot = adb_cmd("reboot:");
            printf("\n%s\n", reboot);
            break;
        }
        default:
            printf("Invalid option selected.\n");
            break;
    }

}
