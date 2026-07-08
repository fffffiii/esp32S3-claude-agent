#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_lcd_panel_io.h"
#include <esp_psram.h>
#include "esp_lcd_panel_vendor.h"
#include "esp_lcd_panel_ops.h"
#include "esp_spiffs.h"
#include "driver/spi_master.h"
#include "lvgl.h"
#include "esp_lvgl_port.h"
#include "config.h"
#include "display.h"
#include "iot_button.h"
#include "button_gpio.h"

static const char *TAG = "display";

// 全局变量
static esp_lcd_panel_io_handle_t panel_io_;
static esp_lcd_panel_handle_t panel_;
static lv_display_t *display_;
static lv_obj_t *gif_img_ = NULL;
static lv_obj_t *g_main_screen = NULL;
static button_handle_t s_boot_button_handle = NULL;

// GIF文件路径数组 - 对应字符 a~j
static const char *g_gif_paths[] = {
    "S:/spiffs/a.gif",  // a
    "S:/spiffs/b.gif",  // b
    "S:/spiffs/c.gif",  // c
    "S:/spiffs/d.gif",  // d
    "S:/spiffs/e.gif",  // e
    "S:/spiffs/f.gif",  // f
    "S:/spiffs/g.gif",  // g
    "S:/spiffs/h.gif",  // h
    "S:/spiffs/i.gif",  // i
    "S:/spiffs/j.gif",  // j
};

// 当前显示的GIF索引
static uint8_t g_current_gif_index = 0;

// GIF图像数量
#define GIF_IMAGE_COUNT (sizeof(g_gif_paths) / sizeof(g_gif_paths[0]))

// 挂载 SPIFFS
static esp_err_t init_spiffs(void) {
    ESP_LOGI(TAG, "Initializing SPIFFS");
    esp_vfs_spiffs_conf_t conf = {
        .base_path = "/spiffs",
        .partition_label = "storage",
        .max_files = 5,
        .format_if_mount_failed = true,
    };
    esp_err_t ret = esp_vfs_spiffs_register(&conf);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to mount SPIFFS (%s)", esp_err_to_name(ret));
        return ret;
    }

    size_t total = 0, used = 0;
    esp_spiffs_info("storage", &total, &used);
    ESP_LOGI(TAG, "SPIFFS mounted: %d KB total, %d KB used", total / 1024, used / 1024);
    return ESP_OK;
}

// 显示 GIF 图像
static void show_gif(void) {
    ESP_LOGI(TAG, "Showing GIF image");

    lv_obj_t *main_screen = lv_scr_act();
    if (main_screen == NULL) {
        ESP_LOGE(TAG, "Failed to get active screen");
        return;
    }

    if (lvgl_port_lock(-1)) {
        if (gif_img_ != NULL) {
            lv_obj_del(gif_img_);
            gif_img_ = NULL;
        }

        gif_img_ = lv_gif_create(main_screen);
        if (gif_img_ == NULL) {
            ESP_LOGE(TAG, "Failed to create GIF object");
            lvgl_port_unlock();
            return;
        }

        lv_gif_set_src(gif_img_, g_gif_paths[g_current_gif_index]);
        lv_obj_center(gif_img_);
        lv_refr_now(NULL);
        lvgl_port_unlock();

        ESP_LOGI(TAG, "GIF displayed: %s", g_gif_paths[g_current_gif_index]);
    } else {
        ESP_LOGE(TAG, "Failed to lock LVGL");
    }
}

// 显示数字123
static void show_number(void) {
    ESP_LOGI(TAG, "Showing number 123");

    lv_obj_t *label = lv_label_create(lv_scr_act());
    if (label == NULL) {
        ESP_LOGE(TAG, "Failed to create label object");
        return;
    }

    lv_label_set_text(label, "123");

    static lv_style_t label_style;
    lv_style_init(&label_style);
    lv_style_set_text_font(&label_style, &lv_font_montserrat_14);
    lv_style_set_text_color(&label_style, lv_color_hex(0x000000));
    lv_obj_add_style(label, &label_style, LV_STATE_DEFAULT);

    lv_obj_center(label);

    ESP_LOGI(TAG, "Number 123 displayed successfully");
}

// 初始化 SPI 外设
static esp_err_t initialize_spi(void) {
    ESP_LOGI(TAG, "Initializing SPI");
    spi_bus_config_t buscfg = {};
    buscfg.mosi_io_num = LCD_MOSI_PIN;
    buscfg.miso_io_num = LCD_MISO_PIN;
    buscfg.sclk_io_num = LCD_SCLK_PIN;
    buscfg.quadwp_io_num = GPIO_NUM_NC;
    buscfg.quadhd_io_num = GPIO_NUM_NC;
    buscfg.max_transfer_sz = DISPLAY_WIDTH * DISPLAY_HEIGHT * sizeof(uint16_t);
    return spi_bus_initialize(SPI3_HOST, &buscfg, SPI_DMA_CH_AUTO);
}

/* 初始化 Boot 按键 — 只创建按钮，不注册业务回调。
 * 按钮的单击/长按回调由 main.c 统一注册。 */
static esp_err_t initialize_boot_button(void) {
    ESP_LOGI(TAG, "Initializing Boot button");

    if (s_boot_button_handle) {
        (void)iot_button_delete(s_boot_button_handle);
        s_boot_button_handle = NULL;
    }

    button_config_t button_cfg = {
        .long_press_time = 1500,
        .short_press_time = 180,
    };

    button_gpio_config_t gpio_cfg = {
        .gpio_num = BOOT_BUTTON_GPIO,
        .active_level = 0,
        .enable_power_save = false,
        .disable_pull = false,
    };

    esp_err_t ret = iot_button_new_gpio_device(&button_cfg, &gpio_cfg, &s_boot_button_handle);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to create boot button: %s", esp_err_to_name(ret));
        return ret;
    }

    ESP_LOGI(TAG, "Boot button initialized successfully on GPIO %d (callbacks deferred to main)", BOOT_BUTTON_GPIO);
    return ESP_OK;
}

// 初始化 LCD 显示
static esp_err_t initialize_lcd(void) {
    ESP_LOGI(TAG, "Initializing LCD display");

    ESP_LOGD(TAG, "Initializing backlight");
    gpio_config_t backlight_config = {
        .pin_bit_mask = (1ULL << DISPLAY_BACKLIGHT_PIN),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE
    };
    esp_err_t ret = gpio_config(&backlight_config);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to configure backlight pin: %s", esp_err_to_name(ret));
        return ret;
    }

    uint32_t backlight_level = DISPLAY_BACKLIGHT_OUTPUT_INVERT ? 0 : 1;
    ret = gpio_set_level(DISPLAY_BACKLIGHT_PIN, backlight_level);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to set backlight level: %s", esp_err_to_name(ret));
        return ret;
    }
    ESP_LOGI(TAG, "Backlight initialized and turned on");

    ESP_LOGD(TAG, "Install panel IO");
    esp_lcd_panel_io_spi_config_t io_config = {};
    io_config.cs_gpio_num = LCD_CS_PIN;
    io_config.dc_gpio_num = LCD_DC_PIN;
    io_config.spi_mode = 0;
    io_config.pclk_hz = 40 * 1000 * 1000;
    io_config.trans_queue_depth = 10;
    io_config.lcd_cmd_bits = 8;
    io_config.lcd_param_bits = 8;
    ret = esp_lcd_new_panel_io_spi(SPI3_HOST, &io_config, &panel_io_);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to create panel IO: %s", esp_err_to_name(ret));
        return ret;
    }

    ESP_LOGD(TAG, "Install LCD driver");
    esp_lcd_panel_dev_config_t panel_config = {};
    panel_config.reset_gpio_num = LCD_RST_PIN;
    panel_config.rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB;
    panel_config.bits_per_pixel = 16;
    ret = esp_lcd_new_panel_st7789(panel_io_, &panel_config, &panel_);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to create panel driver: %s", esp_err_to_name(ret));
        return ret;
    }

    ret = esp_lcd_panel_reset(panel_);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to reset panel: %s", esp_err_to_name(ret));
        return ret;
    }

    ret = esp_lcd_panel_init(panel_);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to initialize panel: %s", esp_err_to_name(ret));
        return ret;
    }

    ret = esp_lcd_panel_invert_color(panel_, DISPLAY_INVERT_COLOR);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to invert color: %s", esp_err_to_name(ret));
        return ret;
    }

    ret = esp_lcd_panel_swap_xy(panel_, DISPLAY_SWAP_XY);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to swap XY: %s", esp_err_to_name(ret));
        return ret;
    }

    ret = esp_lcd_panel_mirror(panel_, DISPLAY_MIRROR_X, DISPLAY_MIRROR_Y);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to mirror display: %s", esp_err_to_name(ret));
        return ret;
    }

    // 绘制白色背景
    uint16_t *buffer = (uint16_t *)malloc(DISPLAY_WIDTH * sizeof(uint16_t));
    if (buffer == NULL) {
        ESP_LOGE(TAG, "Failed to allocate buffer");
        return ESP_ERR_NO_MEM;
    }

    for (int i = 0; i < DISPLAY_WIDTH; i++) {
        buffer[i] = 0xFFFF;
    }

    for (int y = 0; y < DISPLAY_HEIGHT; y++) {
        ret = esp_lcd_panel_draw_bitmap(panel_, 0, y, DISPLAY_WIDTH, y + 1, buffer);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "Failed to draw bitmap: %s", esp_err_to_name(ret));
            free(buffer);
            return ret;
        }
    }

    free(buffer);

    ESP_LOGI(TAG, "Turning display on");
    ret = esp_lcd_panel_disp_on_off(panel_, true);
    if (ret == ESP_ERR_NOT_SUPPORTED) {
        ESP_LOGW(TAG, "Panel does not support disp_on_off; assuming ON");
    } else if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to turn on display: %s", esp_err_to_name(ret));
        return ret;
    }

    return ESP_OK;
}

// 初始化 LVGL
static esp_err_t initialize_lvgl(void) {
    ESP_LOGI(TAG, "Initialize LVGL library");
    lv_init();

#if CONFIG_SPIRAM
    size_t psram_size_mb = esp_psram_get_size() / 1024 / 1024;
    if (psram_size_mb >= 8) {
        lv_image_cache_resize(2 * 1024 * 1024, true);
        ESP_LOGI(TAG, "Use 2MB of PSRAM for image cache");
    } else if (psram_size_mb >= 2) {
        lv_image_cache_resize(512 * 1024, true);
        ESP_LOGI(TAG, "Use 512KB of PSRAM for image cache");
    }
#endif

    ESP_LOGI(TAG, "Initialize LVGL port");
    lvgl_port_cfg_t port_cfg = ESP_LVGL_PORT_INIT_CONFIG();
    port_cfg.task_priority = 1;
#if CONFIG_SOC_CPU_CORES_NUM > 1
    port_cfg.task_affinity = 1;
#endif
    lvgl_port_init(&port_cfg);

    ESP_LOGI(TAG, "Adding LCD display");
    lvgl_port_display_cfg_t display_cfg = {
        .io_handle = panel_io_,
        .panel_handle = panel_,
        .control_handle = NULL,
        .buffer_size = (uint32_t)(DISPLAY_WIDTH * 20),
        .double_buffer = false,
        .trans_size = 0,
        .hres = (uint32_t)DISPLAY_WIDTH,
        .vres = (uint32_t)DISPLAY_HEIGHT,
        .monochrome = false,
        .rotation = {
            .swap_xy = DISPLAY_SWAP_XY,
            .mirror_x = DISPLAY_MIRROR_X,
            .mirror_y = DISPLAY_MIRROR_Y,
        },
        .color_format = LV_COLOR_FORMAT_RGB565,
        .flags = {
            .buff_dma = 1,
            .buff_spiram = 0,
            .sw_rotate = 0,
            .swap_bytes = 1,
            .full_refresh = 0,
            .direct_mode = 0,
        },
    };

    display_ = lvgl_port_add_disp(&display_cfg);
    if (display_ == NULL) {
        ESP_LOGE(TAG, "Failed to add display");
        return ESP_FAIL;
    }

    if (DISPLAY_OFFSET_X != 0 || DISPLAY_OFFSET_Y != 0) {
        lv_display_set_offset(display_, DISPLAY_OFFSET_X, DISPLAY_OFFSET_Y);
    }

    return ESP_OK;
}

/* 根据字符切换GIF图像 */
esp_err_t display_switch_gif_by_char(char ch) {
    ESP_LOGI(TAG, "Switching GIF by character: %c", ch);

    if (ch < 'a' || ch >= 'a' + GIF_IMAGE_COUNT) {
        ESP_LOGE(TAG, "Invalid character for GIF switch: %c, must be a-%c", ch, 'a' + GIF_IMAGE_COUNT - 1);
        return ESP_ERR_INVALID_ARG;
    }

    g_current_gif_index = ch - 'a';

    show_gif();

    return ESP_OK;
}

// 显示屏初始化函数，供外部调用
esp_err_t display_init(void) {
    ESP_LOGI(TAG, "Starting display initialization");

    // 挂载 SPIFFS
    esp_err_t ret = init_spiffs();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to mount SPIFFS: %s", esp_err_to_name(ret));
        return ret;
    }

    // 初始化 SPI
    ret = initialize_spi();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to initialize SPI: %s", esp_err_to_name(ret));
        return ret;
    }
    ESP_LOGI(TAG, "SPI initialized successfully");

    // 初始化 LCD
    ret = initialize_lcd();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to initialize LCD: %s", esp_err_to_name(ret));
        return ret;
    }
    ESP_LOGI(TAG, "LCD initialized successfully");

    // 初始化 LVGL
    ret = initialize_lvgl();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to initialize LVGL: %s", esp_err_to_name(ret));
        return ret;
    }
    ESP_LOGI(TAG, "LVGL initialized successfully");

    g_main_screen = lv_scr_act();
    if (g_main_screen == NULL) {
        ESP_LOGE(TAG, "Failed to get active screen");
        return ESP_FAIL;
    }

    if (lvgl_port_lock(1000)) {
        static lv_style_t style;
        lv_style_init(&style);
        lv_style_set_bg_color(&style, lv_color_white());
        lv_obj_add_style(g_main_screen, &style, LV_STATE_DEFAULT);

        lv_refr_now(NULL);
        lvgl_port_unlock();
    }

    show_gif();

    ret = initialize_boot_button();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to initialize boot button: %s", esp_err_to_name(ret));
    }

    ESP_LOGI(TAG, "Display initialization completed successfully");
    return ESP_OK;
}

// 获取 BOOT 按钮句柄
button_handle_t display_get_boot_button(void) {
    return s_boot_button_handle;
}
