#utils.py
import numpy as np

def find_max(dic={}, config={}):
    keys = list(dic.keys())
    for key in keys:
        if '/W:' in key and 'conv1' not in key and 'fct' not in key:
            name_scope, device = key.split('/W')
            max_val_name = name_scope + '/maxW' + device

            if eval(config['add_reg_prefix']) == True:
                max_val_name = 'regularize_cost_internals/' + max_val_name

            if max_val_name not in keys:
                max_val = np.amax(np.absolute(dic[key]))
                dic[max_val_name] = max_val
    return dic


def make_mask(dic={}, config=[]):
    inBIT, exBIT = eval(config['quantizer']['W_opts']['threshold_bit'])
    ratio = (1 / (1 + ((2 ** exBIT - 1) / (2 ** inBIT - 1))))

    keys = list(dic.keys())
    for key in keys:
        if '/W:' in key and 'conv1' not in key and 'fct' not in key:
            name_scpoe, device = key.split('/W')

            if eval(config['load']['del_reg_prefix']) == True:
                max_val_name = name_scope + '/maxW' + device
            else:
                max_val_name = 'regularize_cost_internals/' + name_scope + '/maxW' + device
            max_val = dic[max_val_name]

            threshold = max_val * ratio

            mask_name = name_scope + '/maskW' + device
            if mask_name not in keys:
                mask = np.where(np.absolute(dic[key]) < threshold, 1., 0.)
                dic[mask_name] = mask
    return dic


def clustering(dic={}, bitW=8, is_Lv=False):
    keys = list(dic.keys())
    for key in keys:
        if '/W:' in key and 'conv1' not in key and 'fct' not in key:
            name_scpoe, device = key.split('/W')

            max_val_name = name_scope + '/maxW' + device
            max_val = dic[max_val_name]

            x = dic[key] / max_val

            if is_Lv:
                n = float(2 ** (bitW - 1) - 1)
                x = np.rint(x * n)
            else:
                n = float(2 ** (bitW - 1) - 0.5)
                x = (np.floor(x * n) + 0.5)
            n_ls = np.range(-n, n + 1)

            cluster_mask_name = name_scope + '/cluster_maskW' + device
            cluster_mask = np.copy(x)

            x = x / n * max_val

            dic[key] = x
            dic[cluster_mask_name] = cluster_mask
    return dic, n_ls