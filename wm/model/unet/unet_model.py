import numpy as np
import torch
import torch.nn.functional as f
from collections import OrderedDict

from wm.model.unet.encoder_decoder import UnetEncoderDecoder
from wm.model.hidden.discriminator import Discriminator
from wm.noise.noiser import Noiser
from wm.train.loss_names import LossNames


class UnetModel:
    def __init__(self, config: dict, **kwargs):
        super(UnetModel, self).__init__()

        self.device = torch.device(config['device'])
        noiser = Noiser(config['noise'])
        noiser.to(self.device)

        self.encoder_decoder = UnetEncoderDecoder(network_variant=config['main_command'], 
                noiser=noiser, message_length=config['message'], 
                encoder_down_blocks=config['encoder_blocks'],
                decoder_blocks=config['decoder_blocks'],
                decoder_inner_channels=config['decoder_channels'],
                decoder_block_type=config['decoder_block_type']).to(self.device)

        self.discriminator = Discriminator(inner_channels=config['discriminator_channels'], block_count=config['discriminator_blocks']).to(self.device)
        self.optimizer_enc_dec = torch.optim.Adam(self.encoder_decoder.parameters())
        self.optimizer_discrim = torch.optim.Adam(self.discriminator.parameters())

        self.config = config
        self.cover_label = 1
        self.encoded_label = 0
        self.tb_logger = kwargs.pop('tb_logger', None)

        # if self.tb_logger is not None:
        #     for module in self.encoder_decoder.encoder._modules.values():
        #         if module.module_name in ['innermost', 'module-3', 'module-4', 'outermost']:
        #             module.weight.register_hook(self.tb_logger.grad_hook_by_name(f'grads/encoder-{module.module_name}'))
        #     if self.config['main_command'] != 'unet-down':
        #         decoder_final = self.encoder_decoder.decoder._modules['linear']
        #         decoder_final.weight.register_hook(self.tb_logger.grad_hook_by_name('grads/decoder_out'))
        #         discrim_final = self.discriminator._modules['linear']
        #         discrim_final.weight.register_hook(self.tb_logger.grad_hook_by_name('grads/discrim_out'))

    def train_on_batch(self, images: torch.Tensor, messages: torch.Tensor):
        """
        Trains the network on a single batch consisting of images and messages
        :param images: training images
        :param messages: training messages
        :return: dictionary of error metrics from Encoder, Decoder, and Discriminator on the current batch
        """
        batch_size = images.shape[0]
        self.encoder_decoder.train()
        self.discriminator.train()
        with torch.enable_grad():
            # ---------------- Train the discriminator -----------------------------
            self.optimizer_discrim.zero_grad()
            # train on cover
            d_target_label_cover = torch.full((batch_size, 1), self.cover_label, device=self.device)
            d_target_label_encoded = torch.full((batch_size, 1), self.encoded_label, device=self.device)
            g_target_label_encoded = torch.full((batch_size, 1), self.cover_label, device=self.device)

            d_on_cover = self.discriminator(images)
            d_loss_on_cover = f.binary_cross_entropy_with_logits(d_on_cover, d_target_label_cover)
            d_loss_on_cover.backward()

            # train on fake
            encoded_images, noised_images, decoded_messages = self.encoder_decoder(images, messages)
            d_on_encoded = self.discriminator(encoded_images.detach())
            d_loss_on_encoded = f.binary_cross_entropy_with_logits(d_on_encoded, d_target_label_encoded)

            d_loss_on_encoded.backward()
            self.optimizer_discrim.step()

            # --------------Train the generator (encoder-decoder) ---------------------
            self.optimizer_enc_dec.zero_grad()
            # target label for encoded images should be 'cover', because we want to fool the discriminator
            d_on_encoded_for_enc = self.discriminator(encoded_images)
            g_loss_adv = f.binary_cross_entropy_with_logits(d_on_encoded_for_enc, g_target_label_encoded)

            g_loss_enc = f.mse_loss(encoded_images, images)

            g_loss_dec = f.mse_loss(decoded_messages, messages)
            g_loss = self.config['adv_loss_weight'] * g_loss_adv + self.config['enc_loss_weight'] * g_loss_enc \
                     + self.config.get('dec_loss_weight', 1) * g_loss_dec

            g_loss.backward()
            self.optimizer_enc_dec.step()

        decoded_rounded = decoded_messages.detach().cpu().numpy().round().clip(0, 1)
        bitwise_avg_err = np.sum(np.abs(decoded_rounded - messages.detach().cpu().numpy())) / (
                batch_size * messages.shape[1])

        # losses = OrderedDict({
        #     'unet_loss    ': g_loss.item(),
        #     'encoder_mse  ': g_loss_enc.item(),
        #     'dec_mse      ': g_loss_dec.item(),
        #     'bitwise-error': bitwise_avg_err,
        #     'g_adv_bce    ': g_loss_adv.item(),
        #     'd_cov_bce    ': d_loss_on_cover.item(),
        #     'd_enc_bce    ': d_loss_on_encoded.item()
        # })
        losses = OrderedDict({
            LossNames.unet_loss.value: g_loss.item(),
            LossNames.encoder_mse.value: g_loss_enc.item(),
            LossNames.decoder_mse.value: g_loss_dec.item(),
            LossNames.bitwise.value: bitwise_avg_err,
            LossNames.gen_adv_bce.value: g_loss_adv.item(),
            LossNames.discr_cov_bce.value: d_loss_on_cover.item(),
            LossNames.discr_enc_bce.value: d_loss_on_encoded.item()
        })
        return losses, (encoded_images, noised_images, decoded_messages)



    def validate_on_batch(self, images: torch.Tensor, messages: torch.Tensor):
        batch_size = images.shape[0]

        self.encoder_decoder.eval()
        self.discriminator.eval()
        with torch.no_grad():
            d_target_label_cover = torch.full((batch_size, 1), self.cover_label, device=self.device)
            d_target_label_encoded = torch.full((batch_size, 1), self.encoded_label, device=self.device)
            g_target_label_encoded = torch.full((batch_size, 1), self.cover_label, device=self.device)

            d_on_cover = self.discriminator(images)
            d_loss_on_cover = f.binary_cross_entropy_with_logits(d_on_cover, d_target_label_cover)

            encoded_images, noised_images, decoded_messages = self.encoder_decoder(images, messages)

            d_on_encoded = self.discriminator(encoded_images)
            d_loss_on_encoded = f.binary_cross_entropy_with_logits(d_on_encoded, d_target_label_encoded)

            d_on_encoded_for_enc = self.discriminator(encoded_images)
            g_loss_adv = f.binary_cross_entropy_with_logits(d_on_encoded_for_enc, g_target_label_encoded)

            g_loss_enc = f.mse_loss(encoded_images, images)

            g_loss_dec = f.mse_loss(decoded_messages, messages)
            g_loss = self.config['adv_loss_weight'] * g_loss_adv + self.config['enc_loss_weight'] * g_loss_enc \
                     + self.config.get('dec_loss_weight', 1) * g_loss_dec

        decoded_rounded = decoded_messages.detach().cpu().numpy().round().clip(0, 1)
        bitwise_avg_err = np.sum(np.abs(decoded_rounded - messages.detach().cpu().numpy())) / (
                batch_size * messages.shape[1])

        losses = OrderedDict({
            LossNames.unet_loss.value: g_loss.item(),
            LossNames.encoder_mse.value: g_loss_enc.item(),
            LossNames.decoder_mse.value: g_loss_dec.item(),
            LossNames.bitwise.value: bitwise_avg_err,
            LossNames.gen_adv_bce.value: g_loss_adv.item(),
            LossNames.discr_cov_bce.value: d_loss_on_cover.item(),
            LossNames.discr_enc_bce.value: d_loss_on_encoded.item()
        })
        return losses, (encoded_images, noised_images, decoded_messages)


    def __str__(self):
        return str(self.encoder_decoder)


    def __repr__(self):
        return str(self.encoder_decoder)

