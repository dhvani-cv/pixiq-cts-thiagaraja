import numpy as np
import cv2


def unroll_cone_tip(img, center, radius):
    # warpPolar output: rows = radius direction, cols = angle direction
    # dsize = (width, height) where width = angle, height = radius
    width = 360  # 1 pixel per degree (full 360°)
    height = radius  # radius direction
    dsize = (width, height)

    unrolled = cv2.warpPolar(img, dsize, center, radius, cv2.WARP_POLAR_LINEAR)

    # Rotate 90° so that angle is along width and radius is along height
    unrolled = cv2.rotate(unrolled, cv2.ROTATE_90_COUNTERCLOCKWISE)

    return unrolled
