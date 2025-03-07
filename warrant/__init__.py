import ast
import boto3
import datetime
import re
import requests
from typing import Any, Optional

from envs import env
from jose import jwt, JWTError

from .aws_srp import AWSSRP, ICognitoClient
from .exceptions import TokenVerificationException


def cognito_to_dict(attr_list: list[dict[str, str]], attr_map: Optional[dict[str, str]]=None) -> dict[str, str]:
    if attr_map is None:
        attr_map = {}
    attr_dict = dict()
    for a in attr_list:
        name = a.get('Name')
        value = a.get('Value')
        if value in ['true', 'false']:
            value = ast.literal_eval(value.capitalize())
        name = attr_map.get(name, name)
        attr_dict[name] = value
    return attr_dict


def dict_to_cognito(attributes: dict[str, str], attr_map: Optional[dict[str, str]]=None) -> list[dict[str, str]]:
    """
    :param attributes: Dictionary of User Pool attribute names/values
    :param attr_map: Dictonnary with attributes mapping
    :return: list of User Pool attribute formatted dicts: {'Name': <attr_name>, 'Value': <attr_value>}
    """
    if attr_map is None:
        attr_map = {}
    for k, v in attr_map.items():
        if v in attributes.keys():
            attributes[k] = attributes.pop(v)

    return [{'Name': key, 'Value': value} for key, value in attributes.items()]


def camel_to_snake(camel_str: str) -> str:
    """
    :param camel_str: string
    :return: string converted from a CamelCase to a snake_case
    """
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', camel_str)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()


def snake_to_camel(snake_str: str) -> str:
    """
    :param snake_str: string
    :return: string converted from a snake_case to a CamelCase
    """
    components = snake_str.split('_')
    return ''.join(x.title() for x in components)


class UserObj:

    def __init__(self, username: str, attribute_list: list[str], cognito_obj: dict, metadata: Optional[dict]=None, attr_map: Optional[dict]=None):
        """
        :param username:
        :param attribute_list:
        :param metadata: Dictionary of User metadata
        """
        self.username = username
        self.pk = username
        self._cognito = cognito_obj
        self._attr_map = {} if attr_map is None else attr_map
        self._data = cognito_to_dict(attribute_list, self._attr_map)
        self.sub = self._data.pop('sub', None)
        self.email_verified = self._data.pop('email_verified', None)
        self.phone_number_verified = self._data.pop('phone_number_verified', None)
        self._metadata = {} if metadata is None else metadata

    def __repr__(self) -> str:
        return '<{class_name}: {uni}>'.format(
            class_name=self.__class__.__name__, uni=self.__unicode__())

    def __unicode__(self) -> str:
        return self.username

    def __getattr__(self, name: str) -> Any:
        if name in list(self.__dict__.get('_data', {}).keys()):
            return self._data.get(name)
        if name in list(self.__dict__.get('_metadata', {}).keys()):
            return self._metadata.get(name)

    def __setattr__(self, name: str, value: str) -> None:
        if name in list(self.__dict__.get('_data', {}).keys()):
            self._data[name] = value
        else:
            super(UserObj, self).__setattr__(name, value)

    def save(self, admin: bool=False) -> None:
        if admin:
            self._cognito.admin_update_profile(self._data, self._attr_map)
            return
        self._cognito.update_profile(self._data, self._attr_map)

    def delete(self, admin: bool=False) -> None:
        if admin:
            self._cognito.admin_delete_user()
            return
        self._cognito.delete_user()


class GroupObj:

    def __init__(self, group_data: dict[str, str], cognito_obj: dict):
        """
        :param group_data: a dictionary with information about a group
        :param cognito_obj: an instance of the Cognito class
        """
        self._data = group_data
        self._cognito = cognito_obj
        self.group_name = self._data.pop('GroupName', None)
        self.description = self._data.pop('Description', None)
        self.creation_date = self._data.pop('CreationDate', None)
        self.last_modified_date = self._data.pop('LastModifiedDate', None)
        self.role_arn = self._data.pop('RoleArn', None)
        self.precedence = self._data.pop('Precedence', None)

    def __unicode__(self) -> str:
        return self.group_name

    def __repr__(self) -> str:
        return '<{class_name}: {uni}>'.format(
            class_name=self.__class__.__name__, uni=self.__unicode__())


class Cognito:
    user_class = UserObj
    group_class = GroupObj

    def __init__(
            self, user_pool_id: str, client_id: str, user_pool_region: Optional[str]=None,
            username: Optional[str]=None, id_token: Optional[str]=None, refresh_token: Optional[str]=None,
            access_token: Optional[str]=None, client_secret: Optional[str]=None,
            access_key: Optional[str]=None, secret_key: Optional[str]=None,
            device_key: Optional[str]=None, device_password: Optional[str]=None, device_group_key: Optional[str]=None,
    ):
        """
        :param user_pool_id: Cognito User Pool ID
        :param client_id: Cognito User Pool Application client ID
        :param username: User Pool username
        :param id_token: ID Token returned by authentication
        :param refresh_token: Refresh Token returned by authentication
        :param access_token: Access Token returned by authentication
        :param access_key: AWS IAM access key
        :param secret_key: AWS IAM secret key
        """

        self.user_pool_id = user_pool_id
        self.client_id = client_id
        self.user_pool_region = user_pool_region if user_pool_region else self.user_pool_id.split('_')[0]
        self.username = username
        self.id_token = id_token
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.client_secret = client_secret
        self.token_type = None
        self.custom_attributes = None
        self.base_attributes = None
        self.pool_jwk = None
        self.device_key = device_key
        self.device_group_key = device_group_key
        self.device_password = device_password

        boto3_client_kwargs = {}
        if access_key and secret_key:
            boto3_client_kwargs['aws_access_key_id'] = access_key
            boto3_client_kwargs['aws_secret_access_key'] = secret_key
        if self.user_pool_region:
            boto3_client_kwargs['region_name'] = self.user_pool_region

        self.client = boto3.client('cognito-idp', **boto3_client_kwargs)

    def get_keys(self) -> dict[str, str]:

        if self.pool_jwk:
            return self.pool_jwk
        else:
            # Check for the dictionary in environment variables.
            pool_jwk_env = env('COGNITO_JWKS', {}, var_type='dict')
            if len(pool_jwk_env.keys()) > 0:
                self.pool_jwk = pool_jwk_env
                return self.pool_jwk
            # If it is not there use the requests library to get it
            self.pool_jwk = requests.get(
                'https://cognito-idp.{}.amazonaws.com/{}/.well-known/jwks.json'.format(
                    self.user_pool_region, self.user_pool_id
                )).json()
            return self.pool_jwk

    def get_key(self, kid: str) -> str:
        keys: dict = self.get_keys().get('keys')
        key = list(filter(lambda x: x.get('kid') == kid, keys))
        return key[0]

    def verify_token(self, token: str, id_name: str, token_use: str) -> dict[str, Any]:
        kid = jwt.get_unverified_header(token).get('kid')
        unverified_claims = jwt.get_unverified_claims(token)
        token_use_verified = unverified_claims.get('token_use') == token_use
        if not token_use_verified:
            raise TokenVerificationException('Your {} token use could not be verified.')
        hmac_key = self.get_key(kid)
        try:
            verified = jwt.decode(token, hmac_key, algorithms=['RS256'],
                                  audience=unverified_claims.get('aud'),
                                  issuer=unverified_claims.get('iss'))
        except JWTError:
            raise TokenVerificationException('Your {} token could not be verified.')
        setattr(self, id_name, token)
        return verified

    def get_user_obj(self, username: Optional[str]=None, attribute_list: Optional[list[dict[str, str]]]=None, metadata: Optional[dict]=None,
                     attr_map: Optional[dict]=None) -> UserObj:
        """
        Returns the specified
        :param username: Username of the user
        :param attribute_list: List of tuples that represent the user's
            attributes as returned by the admin_get_user or get_user boto3 methods
        :param metadata: Metadata about the user
        :param attr_map: Dictionary that maps the Cognito attribute names to
        what we'd like to display to the users
        :return:
        """
        return self.user_class(username=username, attribute_list=attribute_list,
                               cognito_obj=self,
                               metadata=metadata, attr_map=attr_map)

    def get_group_obj(self, group_data: dict) -> GroupObj:
        """
        Instantiates the self.group_class
        :param group_data: a dictionary with information about a group
        :return: an instance of the self.group_class
        """
        return self.group_class(group_data=group_data, cognito_obj=self)

    def switch_session(self, session: ICognitoClient) -> None:
        """
        Primarily used for unit testing so we can take advantage of the
        placebo library (https://githhub.com/garnaat/placebo)
        :param session: boto3 session
        :return:
        """
        self.client = session.client('cognito-idp')

    def check_token(self, renew: bool=True) -> bool:
        """
        Checks the exp attribute of the access_token and either refreshes
        the tokens by calling the renew_access_tokens method or does nothing
        :param renew: bool indicating whether to refresh on expiration
        :return: bool indicating whether access_token has expired
        """
        if not self.access_token:
            raise AttributeError('Access Token Required to Check Token')
        now = datetime.datetime.now()
        dec_access_token = jwt.get_unverified_claims(self.access_token)

        if now > datetime.datetime.fromtimestamp(dec_access_token['exp']):
            expired = True
            if renew:
                self.renew_access_token()
        else:
            expired = False
        return expired

    def add_base_attributes(self, **kwargs) -> None:
        self.base_attributes = kwargs

    def add_custom_attributes(self, **kwargs) -> None:
        custom_key = 'custom'
        custom_attributes = {}

        for old_key, value in kwargs.items():
            new_key = custom_key + ':' + old_key
            custom_attributes[new_key] = value

        self.custom_attributes = custom_attributes

    def register(self, username: str, password: str, attr_map: Optional[dict[str, str]]=None) -> dict[str, str]:
        """
        Register the user. Other base attributes from AWS Cognito User Pools
        are  address, birthdate, email, family_name (last name), gender,
        given_name (first name), locale, middle_name, name, nickname,
        phone_number, picture, preferred_username, profile, zoneinfo,
        updated at, website
        :param username: User Pool username
        :param password: User Pool password
        :param attr_map: Attribute map to Cognito's attributes
        :return response: Response from Cognito

        Example response::
        {
            'UserConfirmed': True|False,
            'CodeDeliveryDetails': {
                'Destination': 'string', # This value will be obfuscated
                'DeliveryMedium': 'SMS'|'EMAIL',
                'AttributeName': 'string'
            }
        }
        """
        attributes = self.base_attributes.copy()
        if self.custom_attributes:
            attributes.update(self.custom_attributes)
        cognito_attributes = dict_to_cognito(attributes, attr_map)
        params = {
            'ClientId': self.client_id,
            'Username': username,
            'Password': password,
            'UserAttributes': cognito_attributes
        }
        self._add_secret_hash(params, 'SecretHash')
        response = self.client.sign_up(**params)

        attributes.update(username=username, password=password)
        self._set_attributes(response, attributes)

        response.pop('ResponseMetadata')
        return response

    def admin_confirm_sign_up(self, username: Optional[str]=None) -> None:
        """
        Confirms user registration as an admin without using a confirmation
        code. Works on any user.
        :param username: User's username
        :return:
        """
        if not username:
            username = self.username
        self.client.admin_confirm_sign_up(
            UserPoolId=self.user_pool_id,
            Username=username,
        )

    def confirm_sign_up(self, confirmation_code: str, username: Optional[str]=None) -> None:
        """
        Using the confirmation code that is either sent via email or text
        message.
        :param confirmation_code: Confirmation code sent via text or email
        :param username: User's username
        :return:
        """
        if not username:
            username = self.username
        params = {'ClientId': self.client_id,
                  'Username': username,
                  'ConfirmationCode': confirmation_code}
        self._add_secret_hash(params, 'SecretHash')
        self.client.confirm_sign_up(**params)

    def admin_authenticate(self, password: str) -> None:
        """
        Authenticate the user using admin super privileges
        :param password: User's password
        :return:
        """
        auth_params = {
            'USERNAME': self.username,
            'PASSWORD': password
        }
        self._add_secret_hash(auth_params, 'SECRET_HASH')
        tokens = self.client.admin_initiate_auth(
            UserPoolId=self.user_pool_id,
            ClientId=self.client_id,
            # AuthFlow='USER_SRP_AUTH'|'REFRESH_TOKEN_AUTH'|'REFRESH_TOKEN'|'CUSTOM_AUTH'|'ADMIN_NO_SRP_AUTH',
            AuthFlow='ADMIN_NO_SRP_AUTH',
            AuthParameters=auth_params,
        )

        self.verify_token(tokens['AuthenticationResult']['IdToken'], 'id_token', 'id')
        self.refresh_token = tokens['AuthenticationResult']['RefreshToken']
        self.verify_token(tokens['AuthenticationResult']['AccessToken'], 'access_token', 'access')
        self.token_type = tokens['AuthenticationResult']['TokenType']

    def authenticate(self, password: str) -> None:
        """
        Authenticate the user using the SRP protocol
        :param password: The user's password
        :return:
        """
        # Login
        aws = AWSSRP(username=self.username, password=password, pool_id=self.user_pool_id,
                     client_id=self.client_id, client=self.client,
                     client_secret=self.client_secret,
                     device_key=self.device_key, device_group_key=self.device_group_key, device_password=self.device_password)
        user_tokens = aws.authenticate_user()

        # Retrieve login tokens
        self.verify_token(user_tokens['AuthenticationResult']['IdToken'], 'id_token', 'id')
        self.refresh_token = user_tokens['AuthenticationResult']['RefreshToken']
        self.verify_token(user_tokens['AuthenticationResult']['AccessToken'], 'access_token', 'access')
        self.token_type = user_tokens['AuthenticationResult']['TokenType']

        # Check of we have device information
        if "NewDeviceMetadata" in user_tokens['AuthenticationResult']:
            # Save the device information
            self.device_key = user_tokens['AuthenticationResult']['NewDeviceMetadata']['DeviceKey']
            self.device_group_key = user_tokens['AuthenticationResult']['NewDeviceMetadata']['DeviceGroupKey']

    def authenticate_with_mfa_token(self, password: str, mfaToken: str) -> None:
        """
        Authenticate the user using the SRP protocol
        :param password: The user's password
        :param token: The user's MFA token
        :return:
        """
        # Login
        aws = AWSSRP(username=self.username, password=password, pool_id=self.user_pool_id,
                     client_id=self.client_id, client=self.client,
                     client_secret=self.client_secret,
                     device_key=self.device_key, device_group_key=self.device_group_key, device_password=self.device_password)
        user_tokens = aws.authenticate_user_with_mfa_token(mfaToken=mfaToken)

        # Retrieve login tokens
        self.verify_token(user_tokens['AuthenticationResult']['IdToken'], 'id_token', 'id')
        self.refresh_token = user_tokens['AuthenticationResult']['RefreshToken']
        self.verify_token(user_tokens['AuthenticationResult']['AccessToken'], 'access_token', 'access')
        self.token_type = user_tokens['AuthenticationResult']['TokenType']

        # Check of we have device information
        if "NewDeviceMetadata" in user_tokens['AuthenticationResult']:
            # Save the device information
            self.device_key = user_tokens['AuthenticationResult']['NewDeviceMetadata']['DeviceKey']
            self.device_group_key = user_tokens['AuthenticationResult']['NewDeviceMetadata']['DeviceGroupKey']

    def new_password_challenge(self, password: str, new_password: str) -> None:
        """
        Respond to the new password challenge using the SRP protocol
        :param password: The user's current passsword
        :param new_password: The user's new passsword
        """
        aws = AWSSRP(username=self.username, password=password, pool_id=self.user_pool_id,
                     client_id=self.client_id, client=self.client,
                     client_secret=self.client_secret)
        tokens = aws.set_new_password_challenge(new_password)
        self.id_token = tokens['AuthenticationResult']['IdToken']
        self.refresh_token = tokens['AuthenticationResult']['RefreshToken']
        self.access_token = tokens['AuthenticationResult']['AccessToken']
        self.token_type = tokens['AuthenticationResult']['TokenType']

        # Check of we have device information
        if "NewDeviceMetadata" in tokens['AuthenticationResult']:
            # Save the device information
            self.device_key = tokens['AuthenticationResult']['NewDeviceMetadata']['DeviceKey']
            self.device_group_key = tokens['AuthenticationResult']['NewDeviceMetadata']['DeviceGroupKey']

    def logout(self) -> None:
        """
        Logs the user out of all clients and removes the expires_in,
        expires_datetime, id_token, refresh_token, access_token, and token_type
        attributes
        :return:
        """
        self.client.global_sign_out(
            AccessToken=self.access_token
        )

        self.id_token = None
        self.refresh_token = None
        self.access_token = None
        self.token_type = None

    def admin_update_profile(self, attrs: dict[str, str], attr_map: Optional[dict[str, str]]=None) -> None:
        user_attrs = dict_to_cognito(attrs, attr_map)
        self.client.admin_update_user_attributes(
            UserPoolId=self.user_pool_id,
            Username=self.username,
            UserAttributes=user_attrs
        )

    def update_profile(self, attrs: dict[str, str], attr_map: Optional[dict[str, str]]=None) -> None:
        """
        Updates User attributes
        :param attrs: Dictionary of attribute name, values
        :param attr_map: Dictionary map from Cognito attributes to attribute
        names we would like to show to our users
        """
        user_attrs = dict_to_cognito(attrs, attr_map)
        self.client.update_user_attributes(
            UserAttributes=user_attrs,
            AccessToken=self.access_token
        )

    def get_user(self, attr_map: Optional[dict[str, str]]=None) -> UserObj:
        """
        Returns a UserObj (or whatever the self.user_class is) by using the
        user's access token.
        :param attr_map: Dictionary map from Cognito attributes to attribute
        names we would like to show to our users
        :return:
        """
        user = self.client.get_user(
            AccessToken=self.access_token
        )

        user_metadata = {
            'username': user.get('Username'),
            'id_token': self.id_token,
            'access_token': self.access_token,
            'refresh_token': self.refresh_token,
        }
        return self.get_user_obj(username=self.username,
                                 attribute_list=user.get('UserAttributes'),
                                 metadata=user_metadata, attr_map=attr_map)

    def get_users(self, attr_map: Optional[dict[str, str]]=None) -> list[UserObj]:
        """
        Returns all users for a user pool. Returns instances of the
        self.user_class.
        :param attr_map:
        :return:
        """
        kwargs = {"UserPoolId": self.user_pool_id}

        response = self.client.list_users(**kwargs)
        return [self.get_user_obj(user.get('Username'),
                                  attribute_list=user.get('Attributes'),
                                  metadata={'username': user.get('Username')},
                                  attr_map=attr_map)
                for user in response.get('Users')]

    def admin_get_user(self, attr_map: Optional[dict[str, str]]=None) -> UserObj:
        """
        Get the user's details using admin super privileges.
        :param attr_map: Dictionary map from Cognito attributes to attribute
        names we would like to show to our users
        :return: UserObj object
        """
        user = self.client.admin_get_user(
            UserPoolId=self.user_pool_id,
            Username=self.username)
        user_metadata = {
            'enabled': user.get('Enabled'),
            'user_status': user.get('UserStatus'),
            'username': user.get('Username'),
            'id_token': self.id_token,
            'access_token': self.access_token,
            'refresh_token': self.refresh_token
        }
        return self.get_user_obj(username=self.username,
                                 attribute_list=user.get('UserAttributes'),
                                 metadata=user_metadata, attr_map=attr_map)

    def admin_create_user(self, username: str, temporary_password: str='', attr_map: Optional[dict[str, str]]=None, **kwargs) -> dict:
        """
        Create a user using admin super privileges.
        :param username: User Pool username
        :param temporary_password: The temporary password to give the user.
        Leave blank to make Cognito generate a temporary password for the user.
        :param attr_map: Attribute map to Cognito's attributes
        :param kwargs: Additional User Pool attributes
        :return response: Response from Cognito
        """
        response = self.client.admin_create_user(
            UserPoolId=self.user_pool_id,
            Username=username,
            UserAttributes=dict_to_cognito(kwargs, attr_map),
            TemporaryPassword=temporary_password,
        )
        kwargs.update(username=username)
        self._set_attributes(response, kwargs)

        response.pop('ResponseMetadata')
        return response

    def send_verification(self, attribute: str='email') -> None:
        """
        Sends the user an attribute verification code for the specified attribute name.
        :param attribute: Attribute to confirm
        """
        self.check_token()
        self.client.get_user_attribute_verification_code(
            AccessToken=self.access_token,
            AttributeName=attribute
        )

    def validate_verification(self, confirmation_code: str, attribute: str='email') -> dict:
        """
        Verifies the specified user attributes in the user pool.
        :param confirmation_code: Code sent to user upon intiating verification
        :param attribute: Attribute to confirm
        """
        self.check_token()
        return self.client.verify_user_attribute(
            AccessToken=self.access_token,
            AttributeName=attribute,
            Code=confirmation_code
        )

    def renew_access_token(self) -> None:
        """
        Sets a new access token on the User using the refresh token.
        """
        auth_params = {'REFRESH_TOKEN': self.refresh_token}
        self._add_secret_hash(auth_params, 'SECRET_HASH')
        if self.device_key is not None:
            auth_params["DEVICE_KEY"] = self.device_key
        refresh_response = self.client.initiate_auth(
            ClientId=self.client_id,
            AuthFlow='REFRESH_TOKEN',
            AuthParameters=auth_params,
        )

        self._set_attributes(
            refresh_response,
            {
                'access_token': refresh_response['AuthenticationResult']['AccessToken'],
                'id_token': refresh_response['AuthenticationResult']['IdToken'],
                'token_type': refresh_response['AuthenticationResult']['TokenType']
            }
        )

    def initiate_forgot_password(self, email: str) -> dict[str, dict[str, str]]:
        """
        Sends a verification code to the user to use to change their password.
        :returns response: Response from Cognito
        """
        params = {
            'ClientId': self.client_id,
            'Username': email
        }
        self._add_secret_hash(params, 'SecretHash')
        return self.client.forgot_password(**params)

    def delete_user(self) -> None:

        self.client.delete_user(
            AccessToken=self.access_token
        )

    def admin_delete_user(self) -> None:
        self.client.admin_delete_user(
            UserPoolId=self.user_pool_id,
            Username=self.username
        )

    def confirm_forgot_password(self, confirmation_code: str, password: str) -> None:
        """
        Allows a user to enter a code provided when they reset their password
        to update their password.
        :param confirmation_code: The confirmation code sent by a user's request
        to retrieve a forgotten password
        :param password: New password
        """
        params = {'ClientId': self.client_id,
                  'Username': self.username,
                  'ConfirmationCode': confirmation_code,
                  'Password': password
                  }
        self._add_secret_hash(params, 'SecretHash')
        response = self.client.confirm_forgot_password(**params)
        self._set_attributes(response, {'password': password})

    def change_password(self, previous_password: str, proposed_password: str) -> None:
        """
        Change the User password
        """
        self.check_token()
        response = self.client.change_password(
            PreviousPassword=previous_password,
            ProposedPassword=proposed_password,
            AccessToken=self.access_token
        )
        self._set_attributes(response, {'password': proposed_password})

    def _add_secret_hash(self, parameters: dict, key: str) -> None:
        """
        Helper function that computes SecretHash and adds it
        to a parameters dictionary at a specified key
        """
        if self.client_secret is not None:
            secret_hash = AWSSRP.get_secret_hash(self.username, self.client_id,
                                                 self.client_secret)
            parameters[key] = secret_hash

    def _set_attributes(self, response: dict, attribute_dict: dict) -> None:
        """
        Set user attributes based on response code
        :param response: HTTP response from Cognito
        :attribute dict: Dictionary of attribute name and values
        """
        status_code = response.get(
            'HTTPStatusCode',
            response['ResponseMetadata']['HTTPStatusCode']
        )
        if status_code == 200:
            for k, v in attribute_dict.items():
                setattr(self, k, v)

    def get_group(self, group_name: str) -> GroupObj:
        """
        Get a group by a name
        :param group_name: name of a group
        :return: instance of the self.group_class
        """
        response = self.client.get_group(GroupName=group_name,
                                         UserPoolId=self.user_pool_id)
        return self.get_group_obj(response.get('Group'))

    def get_groups(self) -> list[GroupObj]:
        """
        Returns all groups for a user pool. Returns instances of the
        self.group_class.
        :return: list of instances
        """
        response = self.client.list_groups(UserPoolId=self.user_pool_id)
        return [self.get_group_obj(group_data)
                for group_data in response.get('Groups')]

    def can_register_device(self) -> bool:
        """ Check if we can register a device in Cognito
        """
        return self.device_group_key is not None and self.device_password is None

    def register_device(self, device_name: str, remember_device: bool = True) -> str:
        """ Register the device

            :returns device_password (str)
        """
        # Check if we can register
        if not self.can_register_device():
            raise ValueError("Device cannot be registered. This is not enabled in Cognito")

        # 3. Generate random device password, device salt and verifier
        device_password, device_secret_verifier_config = AWSSRP.generate_hash_device(self.device_group_key, self.device_key)
        self.device_password = device_password
        self.client.confirm_device(
            AccessToken=self.access_token,
            DeviceKey=self.device_key,
            DeviceSecretVerifierConfig=device_secret_verifier_config,
            DeviceName=device_name,
        )

        # 4. Remember the device
        self.client.update_device_status(
            AccessToken=self.access_token,
            DeviceKey=self.device_key,
            DeviceRememberedStatus='remembered' if remember_device is True else 'not_remembered'
        )
        return device_password

    def forget_device(self) -> None:
        """ Forget the current device
        """
        self.client.forget_device(
            AccessToken=self.access_token,
            DeviceKey=self.device_key,
        )
