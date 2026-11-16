################################################################################
#
# thingino-aiwatch
#
################################################################################

THINGINO_AIWATCH_SITE_METHOD = local
THINGINO_AIWATCH_SITE = $(BR2_EXTERNAL_THINGINO_PATH)/package/thingino-aiwatch
THINGINO_AIWATCH_LICENSE = MIT
THINGINO_AIWATCH_DEPENDENCIES = thingino-jct thingino-libcurl thingino-send2

# The config holds an API key: readable by root only.
define THINGINO_AIWATCH_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/files/aiwatch \
		$(TARGET_DIR)/usr/sbin/aiwatch
	$(INSTALL) -D -m 0600 $(@D)/files/aiwatch.json \
		$(TARGET_DIR)/etc/aiwatch.json
endef

$(eval $(generic-package))
