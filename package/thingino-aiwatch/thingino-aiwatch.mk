################################################################################
#
# thingino-aiwatch
#
################################################################################

THINGINO_AIWATCH_SITE_METHOD = local
THINGINO_AIWATCH_SITE = $(BR2_EXTERNAL_THINGINO_PATH)/package/thingino-aiwatch
THINGINO_AIWATCH_LICENSE = MIT
THINGINO_AIWATCH_DEPENDENCIES = thingino-jct thingino-libcurl thingino-send2

ifeq ($(BR2_PACKAGE_THINGINO_WEBUI),y)
THINGINO_AIWATCH_DEPENDENCIES += thingino-webui
# The manifest in a/plugins/ is picked up by the web UI's plugin assembly,
# which adds the page to the Services menu.
define THINGINO_AIWATCH_INSTALL_WWW_CMDS
	$(INSTALL) -D -m 0644 $(@D)/files/www/config-aiwatch.html \
		$(TARGET_DIR)/var/www/config-aiwatch.html
	$(INSTALL) -D -m 0644 $(@D)/files/www/a/config-aiwatch.js \
		$(TARGET_DIR)/var/www/a/config-aiwatch.js
	$(INSTALL) -D -m 0755 $(@D)/files/www/x/json-aiwatch.cgi \
		$(TARGET_DIR)/var/www/x/json-aiwatch.cgi
	$(INSTALL) -D -m 0644 $(@D)/files/aiwatch.webui.json \
		$(TARGET_DIR)/var/www/a/plugins/aiwatch.webui.json
endef
endif

# The config holds an API key: readable by root only.
define THINGINO_AIWATCH_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/files/aiwatch \
		$(TARGET_DIR)/usr/sbin/aiwatch
	$(INSTALL) -D -m 0600 $(@D)/files/aiwatch.json \
		$(TARGET_DIR)/etc/aiwatch.json
	$(THINGINO_AIWATCH_INSTALL_WWW_CMDS)
endef

$(eval $(generic-package))
